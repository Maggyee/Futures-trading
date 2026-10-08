"""Minute execution extension; no gateway imports or order routing."""

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

from .calendar import MINUTE, at
from .config import ResearchError, validate_config
from .data import Dataset
from .execution_parameters import ExecutionParameters
from .optimization_rules import afternoon_candidates, cost_check, protection_reason
from .refinements import admissible_entry_price, entry_price_guard, scaled_protection
from .signals import Features, SignalLogic, exit_flags, rank_candidates
from .trailing import advance_trailing, initial_trailing


def fee(meta, day, offset, price, quantity):
    schedules = [
        r
        for r in meta["fees"]
        if r["effective_from"] <= day
        and (not r.get("effective_to") or day <= r["effective_to"])
    ]
    if not schedules:
        raise ResearchError(f"{meta['symbol']}/{day}: 无生效手续费规则")
    rule = sorted(schedules, key=lambda r: r["effective_from"])[-1][offset]
    return (
        quantity
        * rule["value"]
        * (price * meta["value_per_price"] if rule["mode"] == "rate" else 1)
    )


def slipped(price, sign, meta, ticks):
    tick = meta["tick_size"]
    units = price / tick
    rounded = math.ceil(units - 1e-9) if sign > 0 else math.floor(units + 1e-9)
    return round((rounded + sign * ticks) * tick, 10)


def can_fill(bar):
    if bar is None:
        return False, "minute_missing"
    if not bar.tradable:
        return False, "explicit_not_tradable"
    if bar.volume <= 0:
        return False, "zero_volume"
    if bar.low == bar.high and (
        (bar.limit_up is not None and bar.high >= bar.limit_up)
        or (bar.limit_down is not None and bar.low <= bar.limit_down)
    ):
        return False, "locked_limit"
    return True, None


def protective_touch(position, bar):
    """Only called after entry at this minute's open, or for an existing position."""
    sign = position.sign
    trailing = getattr(position, "trailing", None)
    stop_price = trailing["stop_price"] if trailing is not None else position.stop
    stop_reason = protection_reason(position)
    stop_gap = sign * (bar.open - stop_price) <= 0
    target_gap = trailing is None and sign * (bar.open - position.target) >= 0
    # An opening exit precedes later extrema; only its already-known barrier applies.
    if stop_gap:
        return {
            "reason": stop_reason,
            "raw_price": bar.open,
            "flags": [stop_reason],
            "ambiguous": False,
            "gap": True,
            "at_open": True,
        }
    if target_gap:
        return {
            "reason": "fixed_target",
            "raw_price": bar.open,
            "flags": ["fixed_target"],
            "ambiguous": False,
            "gap": True,
            "at_open": True,
        }
    stop = bar.low <= stop_price if sign > 0 else bar.high >= stop_price
    target = trailing is None and (bar.high >= position.target if sign > 0 else bar.low <= position.target)
    flags = [
        name for name, hit in ((stop_reason, stop), ("fixed_target", target)) if hit
    ]
    if stop:
        return {
            "reason": stop_reason,
            "raw_price": stop_price,
            "flags": flags,
            "ambiguous": target,
            "gap": False,
            "at_open": False,
        }
    if target:
        return {
            "reason": "fixed_target",
            "raw_price": position.target,
            "flags": flags,
            "ambiguous": False,
            "gap": False,
            "at_open": False,
        }
    return None


@dataclass
class Position:
    key: str
    meta: dict
    sign: int
    quantity: int
    price: float
    raw_price: float
    opened: object
    entry_day: str
    stop: float
    target: float
    entry_fee: float
    risk: float
    margin: float
    signal: dict
    trailing: dict | None = None


@dataclass
class ContractState:
    name: str = "FLAT"
    pending: dict | None = None
    position: Position | None = None
    cooldown: int = 0
    closed_at: object = None
    previous_pass: bool = False
    consumed: set = field(default_factory=set)


class RiskAllocator:
    def __init__(self, cfg):
        self.cfg = cfg

    def usage(self, states):
        total_risk = total_margin = slots = 0
        groups = {}
        for st in states.values():
            if st.position:
                risk, margin, group = (
                    st.position.risk,
                    st.position.margin,
                    st.position.meta["group"],
                )
            elif st.name == "ENTRY_PENDING":
                risk, margin, group = (
                    st.pending["reserved_risk"],
                    st.pending["reserved_margin"],
                    st.pending["group"],
                )
            else:
                continue
            total_risk += risk
            total_margin += margin
            slots += 1
            usage = groups.setdefault(group, {"risk": 0, "margin": 0})
            usage["risk"] += risk
            usage["margin"] += margin
        return total_risk, total_margin, slots, groups

    def allocate(
        self, meta, price, day, states, equity, maximum=None, remaining_open_lots=None,
        stop_loss_ticks=None,
    ):
        r, s = self.cfg["risk"], self.cfg["strategy"]
        risk_used, margin_used, slots, groups = self.usage(states)
        capital = r["initial_capital"]
        if equity <= 0:
            return 0, 0, 0, ["equity_nonpositive"]
        ticks = s["fixed_ticks"][meta["product"]]
        stop = (stop_loss_ticks if stop_loss_ticks is not None else ticks["stop_loss_ticks"]) * meta["tick_size"] * meta["value_per_price"]
        costs = fee(meta, day, "open", price, 1) + fee(
            meta, day, "close_today", price, 1
        )
        costs += 2 * s["slippage_ticks"] * meta["tick_size"] * meta["value_per_price"]
        per_risk = stop + r["cost_buffer_multiple"] * costs
        per_margin = price * meta["value_per_price"] * meta["margin_rate"]
        group = groups.get(meta["group"], {"risk": 0, "margin": 0})
        fraction = r["group_fractions"].get(meta["group"], 0)
        budget_capital = min(capital, equity)
        budgets = {
            "single_trade_risk": budget_capital * r["trade_risk_fraction"],
            "portfolio_risk": budget_capital * r["portfolio_risk_fraction"] - risk_used,
            "group_risk": budget_capital * r["portfolio_risk_fraction"] * fraction
            - group["risk"],
        }
        capacities = {
            name: math.floor(max(0, value) / per_risk)
            for name, value in budgets.items()
        }
        capacities["margin"] = math.floor(
            max(0, equity * r["margin_fraction"] - margin_used) / per_margin
        )
        capacities["group_margin"] = math.floor(
            max(0, equity * r["margin_fraction"] * fraction - group["margin"])
            / per_margin
        )
        capacities["max_positions"] = (
            r["max_lots_per_contract"] if slots < r["max_positions"] else 0
        )
        capacities["max_lots"] = r["max_lots_per_contract"]
        if maximum is not None:
            capacities["reserved_quantity"] = maximum
        if remaining_open_lots is not None:
            capacities["daily_open_limit"] = remaining_open_lots
        quantity = max(0, min(capacities.values()))
        minimum = meta.get("min_open_lots", 1)
        if 0 < quantity < minimum:
            return 0, 0, 0, ["minimum_open_lots"]
        return (
            quantity,
            quantity * per_risk,
            quantity * per_margin,
            [k for k, v in capacities.items() if v < 1],
        )


class PortfolioBacktest:
    def __init__(self, data, cfg=None, cache=None, features=None):
        self.cfg = cfg or data.cfg
        validate_config(self.cfg)
        self.data = (
            data
            if data.cfg == self.cfg
            else Dataset(data.bars, self.cfg, data.quality, daily=data.daily)
        )
        self.calendar = self.data.calendar
        self.features = features if features is not None else Features(self.data, cache)
        # Same feature/data object can be reused; experiment-specific filters use its explicit config.
        self.logic = SignalLogic(self.data, self.features)
        self.parameters = self.make_parameters()
        self.allocator = RiskAllocator(self.cfg)
        self.states = {}
        self.cash = self.cfg["risk"]["initial_capital"]
        self.marks = {}
        self.trades, self.orders, self.signals, self.events = [], [], [], []
        self.unselected_observations_skipped = 0
        self.pools, self.excluded, self.candidates, self.equity = [], [], [], []
        self.candidate_execution = []
        self.opened_lots = defaultdict(int)
        self.stopped_sides = set()
        self.unflattened = []
        self.break_risk = []
        self.ambiguities = 0

    def make_parameters(self):
        return ExecutionParameters(self.cfg, self.data.metadata)

    def state(self, key):
        return self.states.setdefault(key, ContractState())

    def update_candidates(self, candidates, current, day, cutoff):
        """Extension point; the original fixed Top-K selection is unchanged."""

    def position_exit_flags(self, key, bar, one, past, direction):
        return exit_flags(one, past, direction, self.cfg)[0]

    def entry_protection(self, signal, meta, roundtrip_fees, minimum_stop_ticks=0, price=None):
        return scaled_protection(signal, meta, self.cfg["strategy"], roundtrip_fees, minimum_stop_ticks)

    def entry_trigger(self, key, evaluation):
        st = self.state(key)
        passing, pullback = evaluation["all_pass"], evaluation["pullback"]
        if self.cfg["strategy"]["entry_mode"] == "direct":
            trigger = passing and not st.previous_pass
        else:
            trigger = passing and pullback is not None and pullback["event"] not in st.consumed
        st.previous_pass = passing
        return trigger

    def opportunity_priority(self, opportunity):
        signal = opportunity[0]
        return signal["rank"], -abs(signal["r8"]), signal["group"], signal["contract"]

    def equity_value(self):
        return self.cash + sum(
            p.sign
            * (self.marks.get(p.key, p.price) - p.price)
            * p.meta["value_per_price"]
            * p.quantity
            for st in self.states.values()
            if (p := st.position)
        )

    def event(self, time, key, action, **details):
        self.events.append(
            {"time": time.isoformat(), "contract": key, "action": action, **details}
        )

    def request_exit(self, key, time, flags):
        st = self.state(key)
        if not st.position:
            return
        if st.name == "EXIT_PENDING":
            st.pending["flags"] = list(dict.fromkeys(st.pending["flags"] + flags))
            return
        st.name = "EXIT_PENDING"
        st.pending = {"signal_time": time.isoformat(), "flags": flags}
        self.event(time, key, "exit_requested", flags=flags)

    def close(self, key, bar, raw_price, time, flags, ambiguous=False, gap=False):
        st, s = self.state(key), self.cfg["strategy"]
        p = st.position
        meta, rejections = self.parameters.resolve(key, time)
        if rejections or meta is None:
            self.request_exit(key, time, flags)
            self.event(time, key, "exit_execution_unavailable", rejections=rejections)
            return False
        price = slipped(raw_price, -p.sign, meta, s["slippage_ticks"])
        offset = "close_today" if p.entry_day == bar.trading_day else "close_yesterday"
        commission = fee(meta, bar.trading_day, offset, price, p.quantity)
        gross = p.sign * (price - p.price) * p.meta["value_per_price"] * p.quantity
        self.cash += gross - commission
        precedence = [
            "time_force",
            "session_close",
            "break_close",
            "fixed_stop",
            "breakeven_stop",
            "trailing_stop",
            "fixed_target",
            "volume",
            "ma40_cross",
            "ma40_approach",
        ]
        reason = next((r for r in precedence if r in flags), flags[0])
        exit_signal = (
            st.pending["signal_time"] if st.name == "EXIT_PENDING" else time.isoformat()
        )
        slip = (
            (abs(p.price - p.raw_price) + abs(price - raw_price))
            * p.meta["value_per_price"]
            * p.quantity
        )
        self.trades.append(
            {
                "id": len(self.trades) + 1,
                "contract": key,
                "product": p.meta["product"],
                "group": p.meta["group"],
                "direction": "LONG" if p.sign > 0 else "SHORT",
                "quantity": p.quantity,
                "entry_signal_time": p.signal["time"],
                "entry_time": p.opened.isoformat(),
                "exit_signal_time": exit_signal,
                "exit_time": time.isoformat(),
                "entry_price": p.price,
                "exit_price": price,
                "stop_price": p.stop,
                "target_price": p.target,
                "entry_fee": p.entry_fee,
                "exit_fee": commission,
                "fees": p.entry_fee + commission,
                "slippage_cost_diagnostic": slip,
                "gross_pnl": gross,
                "net_pnl": gross - p.entry_fee - commission,
                "exit_offset": offset,
                "exit_reason": reason,
                "exit_flags": flags,
                "ambiguous_bar": ambiguous,
                "gap": gap,
                "holding_minutes": self.calendar.holding_minutes(
                    p.opened, time, p.meta
                ),
                "entry_mode": s["entry_mode"],
                "pullback": p.signal["pullback"],
                "rank": p.signal["rank"],
                "r8": p.signal["r8"],
                "entry_snapshot": p.signal["snapshot"],
                "entry_execution_reference": p.meta.get("execution_reference"),
                "exit_execution_reference": meta.get("execution_reference"),
            }
        )
        if "entry_protection" in p.signal:
            self.trades[-1]["entry_protection"] = p.signal["entry_protection"]
            self.trades[-1]["entry_allocation"] = p.signal["entry_allocation"]
        if p.trailing is not None:
            self.trades[-1]["trailing_exit"] = dict(p.trailing)
        if "fill_cost_check" in p.signal:
            self.trades[-1]["entry_cost_check"] = p.signal["fill_cost_check"]
        self.ambiguities += int(ambiguous)
        self.event(
            time, key, "exit_filled", price=price, quantity=p.quantity, flags=flags
        )
        if (s.get("block_same_day_reentry_after_stop", False)
                and reason == "fixed_stop" and gross - p.entry_fee - commission < 0):
            direction = "LONG" if p.sign > 0 else "SHORT"
            self.stopped_sides.add((bar.trading_day, key, direction))
            self.event(time, key, "same_day_reentry_blocked", direction=direction,
                       trigger_trade_id=self.trades[-1]["id"])
        st.position, st.pending = None, None
        st.name = "COOLDOWN" if s["cooldown_minutes"] else "FLAT"
        st.cooldown, st.closed_at = s["cooldown_minutes"], time
        return True

    def timer(self, time, day, active_meta):
        s = self.cfg["strategy"]
        for key, meta in active_meta.items():
            st = self.state(key)
            cutoff, force, close = self.calendar.deadlines(day, meta, s["times"])
            if (
                st.name == "COOLDOWN"
                and st.closed_at
                and time > st.closed_at
                and self.calendar.locate(time - MINUTE, day, meta)
            ):
                st.cooldown -= 1
                if st.cooldown <= 0:
                    st.name = "FLAT"
            if time >= cutoff and st.name == "ENTRY_PENDING":
                self.event(
                    time,
                    key,
                    "entry_cancelled",
                    reason="entry_cutoff",
                    signal_time=st.pending["signal"]["time"],
                )
                st.pending = None
                st.name = "FLAT"
            flags = []
            if time >= force:
                flags.append("time_force")
            if time >= close:
                flags.append("session_close")
            if not s["allow_hold_across_break"] and self.calendar.in_break(
                time + MINUTE, day, meta
            ):
                flags.append("break_close")
                if st.name == "ENTRY_PENDING":
                    self.event(time, key, "entry_cancelled", reason="break_close")
                    st.pending, st.name = None, "FLAT"
            if flags:
                self.request_exit(key, time, flags)
            if (
                not s["allow_hold_across_break"]
                and self.calendar.in_break(time, day, meta)
                and st.position
            ):
                record = {
                    "date": day,
                    "time": time.isoformat(),
                    "contract": key,
                    "quantity": st.position.quantity,
                    "reason": "unable_to_flatten_before_break",
                }
                self.break_risk.append(record)
                self.event(
                    time, key, "unflattened_break_risk", quantity=st.position.quantity
                )

    def fill_open(self, key, bar, time):
        st = self.state(key)
        ok, reason = can_fill(bar)
        if st.name in {"ENTRY_PENDING", "EXIT_PENDING"} and not ok:
            self.event(time, key, "fill_unavailable", state=st.name, reason=reason)
            return
        if st.name == "EXIT_PENDING":
            self.close(key, bar, bar.open, time, st.pending["flags"])
        elif st.name == "ENTRY_PENDING":
            pending, s = st.pending, self.cfg["strategy"]
            meta, rejections = self.parameters.resolve(key, time)
            signal = pending["signal"]
            if rejections or meta is None:
                self.event(
                    time,
                    key,
                    "entry_cancelled",
                    reason="fill_execution_recheck",
                    rejections=rejections,
                )
                signal["fill_execution_rejections"] = rejections
                st.name, st.pending, st.previous_pass = "FLAT", None, False
                return
            sign = 1 if signal["direction"] == "LONG" else -1
            price = slipped(bar.open, sign, meta, s["slippage_ticks"])
            if s.get("entry_cost_filter"):
                costs = fee(meta, bar.trading_day, "open", price, 1) + fee(meta, bar.trading_day, "close_today", price, 1)
                signal["fill_cost_check"] = cost_check(signal, meta, s, costs, price)
                if not signal["fill_cost_check"]["accepted"]:
                    st.name, st.pending, st.previous_pass = "FLAT", None, False
                    self.event(time, key, "entry_cancelled", reason="fill_cost_recheck")
                    return
            if guard := pending.get("price_guard"):
                accepted = admissible_entry_price(guard, price)
                signal["fill_price_check"] = {
                    **guard, "opening_time": time.isoformat(), "raw_open": bar.open,
                    "modeled_price": price, "accepted": accepted,
                }
                if not accepted:
                    signal["fill_price_rejections"] = ["outside_signal_price_limit"]
                    st.name, st.pending, st.previous_pass = "FLAT", None, True
                    self.event(time, key, "entry_cancelled", reason="fill_price_recheck", **signal["fill_price_check"])
                    return
            protection = None
            if s.get("protection_scale"):
                estimated_fees = fee(meta, bar.trading_day, "open", price, 1) + fee(meta, bar.trading_day, "close_today", price, 1)
                protection = self.entry_protection(signal, meta, estimated_fees, pending["protection"]["stop_loss_ticks"], price)
                if protection.get("accepted") is False:
                    signal["fill_structure_rejections"] = protection["rejections"]
                    st.name, st.pending, st.previous_pass = "FLAT", None, False
                    self.event(time, key, "entry_cancelled", reason="fill_structure_recheck", rejections=protection["rejections"])
                    return
            st.name, st.pending = (
                "FLAT",
                None,
            )  # Release reservation before rechecking gap/margin.
            quantity, risk, margin, reasons = self.allocator.allocate(
                meta,
                price,
                bar.trading_day,
                self.states,
                self.equity_value(),
                pending["quantity"],
                self.remaining_open_lots(meta, bar.trading_day),
                stop_loss_ticks=protection["stop_loss_ticks"] if protection else None,
            )
            if quantity == 0:
                st.previous_pass = False
                self.event(
                    time,
                    key,
                    "entry_cancelled",
                    reason="fill_risk_recheck",
                    rejections=reasons,
                )
                signal["fill_rejections"] = reasons
                return
            ticks = protection or s["fixed_ticks"][meta["product"]]
            stop = round(
                price - sign * ticks["stop_loss_ticks"] * meta["tick_size"], 10
            )
            target = round(
                price + sign * ticks["take_profit_ticks"] * meta["tick_size"], 10
            )
            if protection is not None:
                signal["entry_protection"] = protection
                signal["entry_allocation"] = {
                    "equity_before_fee": self.equity_value(), "reserved_quantity": pending["quantity"],
                    "filled_quantity": quantity, "planned_risk": risk, "margin": margin,
                    "single_trade_budget": min(self.cfg["risk"]["initial_capital"], self.equity_value()) * self.cfg["risk"]["trade_risk_fraction"],
                }
            commission = fee(meta, bar.trading_day, "open", price, quantity)
            self.cash -= commission
            self.opened_lots[(bar.trading_day, key)] += quantity
            st.position = Position(
                key,
                meta,
                sign,
                quantity,
                price,
                bar.open,
                time,
                bar.trading_day,
                stop,
                target,
                commission,
                risk,
                margin,
                signal,
            )
            if rule := s.get("trailing_exit"):
                st.position.trailing = initial_trailing(st.position, rule, s.get("breakeven"), s["slippage_ticks"])
            st.name = "LONG" if sign > 0 else "SHORT"
            signal["filled"] = True
            signal["fill_time"] = time.isoformat()
            self.event(
                time,
                key,
                "entry_filled",
                price=price,
                quantity=quantity,
                stop=stop,
                target=target,
            )

    def admit_opportunity(self, signal, meta, price, day, time):
        key, st = signal["contract"], self.state(signal["contract"])
        protection = None
        if self.cfg["strategy"].get("protection_scale"):
            estimated_fees = fee(meta, day, "open", price, 1) + fee(meta, day, "close_today", price, 1)
            protection = self.entry_protection(signal, meta, estimated_fees, price=price)
            if protection.get("accepted") is False:
                signal["risk_pass"], signal["qualified_with_risk"] = False, False
                signal["risk_rejections"] = protection["rejections"]
                st.previous_pass = False
                self.event(time + MINUTE, key, "entry_rejected", reason="structure_protection", rejections=protection["rejections"])
                return
        quantity, risk, margin, reasons = self.allocator.allocate(
            meta,
            price,
            day,
            self.states,
            self.equity_value(),
            remaining_open_lots=self.remaining_open_lots(meta, day),
            stop_loss_ticks=protection["stop_loss_ticks"] if protection else None,
        )
        if (self.unflattened or self.break_risk) and self.cfg["risk"][
            "block_after_unflattened"
        ]:
            quantity, reasons = 0, reasons + ["unflattened_previous_session"]
        signal["risk_pass"] = quantity > 0
        signal["qualified_with_risk"] = quantity > 0
        signal["risk_rejections"] = reasons
        if not quantity:
            # Rejected direct signals may qualify when capacity frees.
            st.previous_pass = False
            return
        st.name = "ENTRY_PENDING"
        st.pending = {
            "signal": signal,
            "quantity": quantity,
            "reserved_risk": risk,
            "reserved_margin": margin,
            "group": meta["group"],
        }
        if self.cfg["strategy"].get("recheck_entry_price", False):
            st.pending["price_guard"] = entry_price_guard(signal, meta, self.cfg["strategy"])
        if protection is not None:
            st.pending["protection"] = protection
            signal["protection_plan"] = protection
        if signal["pullback"]:
            st.consumed.add(signal["pullback"]["event"])
        self.orders.append(
            {
                "time": signal["time"],
                "contract": key,
                "quantity": quantity,
                "direction": signal["direction"],
                "rank": signal["rank"],
                "risk": risk,
                "margin": margin,
            }
        )
        if "price_guard" in st.pending:
            self.orders[-1]["price_guard"] = st.pending["price_guard"]
        if protection is not None:
            self.orders[-1]["protection"] = protection
        self.event(
            time + MINUTE,
            key,
            "entry_requested",
            quantity=quantity,
            rank=signal["rank"],
        )

    def remaining_open_lots(self, meta, day):
        limit = meta.get("daily_open_limit")
        if limit is None:
            return None
        return max(
            0, limit - self.opened_lots[(day, meta["symbol"] + "." + meta["exchange"])]
        )

    def record_account(self, time, ending, day):
        risk, margin, slots, _ = self.allocator.usage(self.states)
        self.equity.append(
            {
                "time": min(time + MINUTE, ending).isoformat(),
                "date": day,
                "equity": self.equity_value(),
                "cash": self.cash,
                "risk": risk,
                "margin": margin,
                "positions": slots,
            }
        )

    def run(self, start, end):
        if not self.data.bars:
            raise ResearchError("该资金场景没有可用历史数据，不能生成回测收益结论")
        if not any(start <= b.trading_day <= end for b in self.data.bars):
            raise ResearchError("回测窗口没有历史分钟，不能用预热数据充当回测样本")
        if self.data.quality.get("errors"):
            raise ResearchError("数据校验有错误，请先执行 validate-data")
        # All rolling values are already causally computed using full contract
        # history. Only latest/past (<=10 observations) is queried during matching.
        # Keep 40 preceding completed rows, without recomputing or resetting SMA/ATR.
        # Copy each slice so historical backing blocks can actually be released.
        for identity, frame in self.features.frames.items():
            if not frame.empty:
                beginning = max(0, frame.index.searchsorted(at(start, "00:00")) - 40)
                self.features.frames[identity] = frame.iloc[beginning:].copy()
        self.parameters.preflight(
            {b.product for b in self.data.bars}, start, end, self.data
        )
        for day in [d for d in self.calendar.days if start <= d <= end]:
            pool, rejected = self.data.pool(day)
            self.pools.extend(
                {k: v for k, v in row.items() if k != "meta"} for row in pool
            )
            self.excluded.extend(rejected)
            active_meta = {
                key: self.data.metadata.get(key, day) for key in self.data.by_contract
            }
            active_meta = {
                k: v
                for k, v in active_meta.items()
                if v and self.calendar.periods(day, v)
            }
            if not active_meta:
                continue
            openings = {self.calendar.bounds(day, m)[0] for m in active_meta.values()}
            beginning, ending = (
                min(openings),
                max(self.calendar.bounds(day, m)[1] for m in active_meta.values()),
            )
            by_key, ranked_groups = {}, set()
            afternoon_ranked = set()
            afternoon_openings = {period[0] for m in active_meta.values() for period in self.calendar.periods(day, m) if period[0].hour >= 12} if self.cfg["strategy"].get("afternoon_rerank") else set()
            time = beginning
            while time <= ending:
                self.timer(time, day, active_meta)
                current = {
                    key: self.data.by_day.get((day, key), {}).get(time)
                    for key in active_meta
                }
                # All contracts share the same opening instant. Recheck pending
                # fills against opening marks, before observing intraminute paths.
                for key, bar in current.items():
                    if bar:
                        self.marks[key] = bar.open
                for key in sorted(active_meta):
                    self.fill_open(key, current[key], time)
                for key in sorted(active_meta):
                    bar = current[key]
                    st = self.state(key)
                    if bar and st.position:
                        hit = protective_touch(st.position, bar)
                        if hit:
                            ok, _ = can_fill(bar)
                            if ok:
                                close_time = time if hit["at_open"] else bar.end
                                self.close(
                                    key,
                                    bar,
                                    hit["raw_price"],
                                    close_time,
                                    hit["flags"],
                                    hit["ambiguous"],
                                    hit["gap"],
                                )
                            else:
                                self.request_exit(key, bar.end, hit["flags"])
                    if bar:
                        self.marks[key] = bar.close
                for opening in sorted(openings):
                    if (
                        time + MINUTE == opening + 8 * MINUTE
                        and opening not in ranked_groups
                    ):
                        subset = [
                            r
                            for r in pool
                            if self.calendar.bounds(day, r["meta"])[0] == opening
                        ]
                        candidates, exclusions = rank_candidates(
                            self.data,
                            day,
                            subset,
                            time + MINUTE,
                            self.cfg["strategy"]["k"],
                        )
                        self.candidates.extend(candidates)
                        self.candidate_execution.extend(
                            self.parameters.candidate(r) for r in candidates
                        )
                        self.excluded.extend(exclusions)
                        by_key.update({r["contract"]: r for r in candidates})
                        ranked_groups.add(opening)
                for opening in sorted(afternoon_openings):
                    if time + MINUTE == opening + 8 * MINUTE and opening not in afternoon_ranked:
                        subset = [r for r in pool if any(a == opening for a, _ in self.calendar.periods(day, r["meta"]))]
                        candidates, exclusions = afternoon_candidates(self.data, day, subset, opening, self.cfg["strategy"]["k"], cutoff=time+MINUTE)
                        for entry in subset:
                            key = entry["contract"]
                            if key in by_key:
                                by_key[key] = {**by_key[key], "selected": False}
                            self.state(key).previous_pass = False
                        by_key.update({r["contract"]: r for r in candidates})
                        self.candidates.extend(candidates)
                        self.candidate_execution.extend(self.parameters.candidate(r) for r in candidates)
                        self.excluded.extend(exclusions)
                        afternoon_ranked.add(opening)
                        self.event(time + MINUTE, "POOL", "afternoon_rerank", candidates=len(candidates), opening=opening.isoformat())
                self.update_candidates(by_key, current, day, time + MINUTE)
                opportunities = []
                for key in sorted(by_key):
                    bar = current.get(key)
                    if not bar:
                        continue
                    st, candidate, meta = self.state(key), by_key[key], active_meta[key]
                    if st.position:
                        one = self.features.latest(key, 1, bar.end)
                        past = self.features.past(key, bar.end, 3)
                        flags = self.position_exit_flags(
                            key, bar, one, past,
                            "LONG" if st.position.sign > 0 else "SHORT",
                        )
                        if getattr(st.position, "trailing", None) is not None and st.name != "EXIT_PENDING":
                            diagnostic = advance_trailing(st.position, bar, float(one.previous_atr) if one is not None else None)
                            self.event(bar.end, key, "trailing_observation", **diagnostic)
                            if diagnostic["close_exit_requested"]:
                                flags.append(protection_reason(st.position))
                        if flags:
                            self.request_exit(key, bar.end, flags)
                    if not candidate["selected"] and not self.cfg.get("storage", {}).get("record_unselected_signals", True):
                        # The candidate gate is always false here. Exit matching above,
                        # ranking, capital and every selected observation remain active.
                        st.previous_pass = False
                        self.unselected_observations_skipped += 1
                        continue
                    cutoff, _, _ = self.calendar.deadlines(
                        day, meta, self.cfg["strategy"]["times"]
                    )
                    evaluation = self.logic.evaluate(
                        bar, candidate, st.name == "FLAT", bar.end < cutoff
                    )
                    execution_meta, execution_rejections = self.parameters.resolve(key, bar.end)
                    if self.cfg["strategy"].get("entry_cost_filter"):
                        if execution_meta is not None and not execution_rejections:
                            costs = fee(execution_meta, day, "open", bar.close, 1) + fee(execution_meta, day, "close_today", bar.close, 1)
                            evaluation["cost_check"] = cost_check({"snapshot": evaluation["snapshot"]}, execution_meta, self.cfg["strategy"], costs, bar.close)
                            accepted_cost = evaluation["cost_check"]["accepted"]
                        else:
                            accepted_cost = False
                        evaluation["filters"]["cost"] = accepted_cost
                        evaluation["all_pass"] = all(evaluation["filters"].values())
                        evaluation["rejections"] = [name for name, value in evaluation["filters"].items() if not value]
                    if self.cfg["strategy"].get("block_same_day_reentry_after_stop", False):
                        evaluation["filters"]["stop_reentry"] = (day, key, candidate["direction"]) not in self.stopped_sides
                        evaluation["all_pass"] = all(evaluation["filters"].values())
                        evaluation["rejections"] = [name for name, value in evaluation["filters"].items() if not value]
                    trigger = self.entry_trigger(key, evaluation)
                    row = {
                        "time": bar.end.isoformat(),
                        "date": day,
                        "contract": key,
                        "product": meta["product"],
                        "group": candidate["group"],
                        "direction": candidate["direction"],
                        "rank": candidate["rank"],
                        "r8": candidate["r8"],
                        **evaluation,
                        "trigger": trigger,
                        "execution_pass": execution_meta is not None
                        and not execution_rejections,
                        "execution_rejections": execution_rejections,
                        "risk_pass": False,
                        "qualified_with_risk": False,
                        "risk_rejections": [],
                        "filled": False,
                    }
                    self.signals.append(row)
                    if trigger and row["execution_pass"]:
                        opportunities.append((row, execution_meta, bar.close))
                # Fixed priority; K never changes capital, caps or per-trade budgets.
                opportunities.sort(key=self.opportunity_priority)
                for signal, meta, price in opportunities:
                    self.admit_opportunity(signal, meta, price, day, time)
                self.record_account(time, ending, day)
                time += MINUTE
            for key, st in self.states.items():
                if st.position:
                    risk = {
                        "date": day,
                        "contract": key,
                        "quantity": st.position.quantity,
                        "state": st.name,
                        "last_mark": self.marks.get(key),
                        "reason": "unable_to_flatten_by_session_close",
                    }
                    self.unflattened.append(risk)
                    self.event(
                        ending,
                        key,
                        "unflattened_risk",
                        **{k: v for k, v in risk.items() if k != "contract"},
                    )
            if self.parameters.mode in {"diagnostic", "price_replay"}:
                print(
                    json.dumps(
                        {
                            "phase": self.parameters.mode + "_day_complete",
                            "date": day,
                            "signals": len(self.signals),
                            "trades": len(self.trades),
                            "unflattened": len(self.unflattened),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return {
            "status": "execution_risk"
            if self.unflattened or self.break_risk
            else "completed",
            "start": start,
            "end": end,
            "provenance": "SYNTHETIC_TEST_ONLY"
            if self.cfg.get("synthetic")
            else "DIAGNOSTIC_MINUTE_BACKTEST"
            if self.parameters.mode == "diagnostic"
            else "MINUTE_HISTORICAL_BACKTEST",
            "execution_mode": self.parameters.mode,
            "execution_qualification": {
                "assumptions": self.parameters.qualification.get("assumptions", []),
                "ranking_policy": self.parameters.qualification.get("ranking_policy"),
                "qualification_hash": self.cfg.get("execution", {}).get(
                    "qualification_hash"
                ),
                "locked_test_read": False
                if self.parameters.mode == "diagnostic"
                else None,
            },
            "trades": self.trades,
            "orders": self.orders,
            "signals": self.signals,
            "unselected_signal_observations_skipped": self.unselected_observations_skipped,
            "events": self.events,
            "daily_pool": self.pools,
            "pool_exclusions": self.excluded,
            "daily_candidates": self.candidates,
            "candidate_execution": self.candidate_execution,
            "equity": self.equity,
            "unflattened_risk": self.unflattened,
            "break_unflattened_risk": self.break_risk,
            "ambiguous_trades": self.ambiguities,
            "open_positions": [
                {
                    "contract": k,
                    "state": st.name,
                    "quantity": st.position.quantity,
                    "entry_price": st.position.price,
                    "stop": st.position.stop,
                    "target": st.position.target,
                }
                for k, st in self.states.items()
                if st.position
            ],
            "feature_cache_key": self.features.cache_key,
        }
