"""Optional confirmed price patterns and frozen five-minute structural risk."""

import math
from datetime import datetime

from .calendar import MINUTE, at
from .execution import PortfolioBacktest, fee
from .refinements import trend_quality
from .signals import finite, pullback_event

REMOVED = {"efficiency", "trend_activity", "trend_displacement",
           "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"}


def structure_context(data, features, bar, direction):
    rule = data.cfg["strategy"].get("structure_protection", {})
    count = rule.get("lookback_bars", 3)
    minutes = rule.get("timeframe_minutes", 5)
    meta = data.metadata.get(bar.key, bar.trading_day)
    period = next(((a, b) for a, b in data.calendar.periods(bar.trading_day, meta)
                   if a <= bar.datetime < b), None)
    past = features.past_period(bar.key, minutes, bar.end, count)
    result = {"signal_time": bar.end.isoformat(), "period_open": period[0].isoformat() if period else None,
              "ready": False, "sources": []}
    if period is None or past is None or len(past) != count:
        return result
    result["sources"] = [{"end": t.isoformat(), "low": float(row.low), "high": float(row.high)}
                         for t, row in past.iterrows()]
    if any(t - minutes * MINUTE < period[0] or t > bar.end for t in past.index):
        return result
    if any(b - a != minutes * MINUTE for a, b in zip(past.index[:-1], past.index[1:], strict=True)):
        return result
    atr = past.iloc[-1].previous_atr
    if not finite(atr) or atr <= 0:
        return result
    result.update(ready=True, atr_previous=float(atr),
                  extreme=float(past.low.min() if direction == "LONG" else past.high.max()),
                  source_end=past.index[-1].isoformat())
    return result


class ConfirmedLogic:
    def __init__(self, original, baseline, journal):
        self.original, self.baseline, self.journal = original, baseline, journal
        self.data, self.features = original.data, original.features
        self.windows, self.setups, self.quality_cache = {}, {}, {}
        self.events = []
        self.setup_clocks = {}

    def transition(self, identity, setup, clock, action, reason, **details):
        self.events.append({"trigger": False, "time": clock.isoformat(), "date": identity[0],
                            "contract": identity[1], "direction": identity[2],
                            "setup_id": setup["setup_end"], "kind": setup["kind"],
                            "action": action, "reason": reason, "setup": dict(setup), **details})

    def finish(self, clock):
        for identity, setup in list(self.setups.items()):
            expiry = setup.get("expires_at", setup["window"]["expires_at"])
            expired = datetime.fromisoformat(expiry) <= clock
            self.transition(identity, setup, datetime.fromisoformat(expiry) if expired else clock,
                            "cancelled", "expired" if expired else "end_of_observation")
        self.setups.clear()
        self.setup_clocks.clear()

    def lifetime(self, bar, candidate, base, higher, period):
        """Review the environment independently of an already formed pattern."""
        rule = self.data.cfg["strategy"]["entry_confirmation"]
        identity = bar.trading_day, bar.key, candidate["direction"]
        sign = 1 if candidate["direction"] == "LONG" else -1
        tick = self.data.metadata.get(bar.key, bar.trading_day)["tick_size"]
        clock, cancellations = bar.end, []
        for old in list(self.setups):
            if old[1] == bar.key and old != identity:
                setup = self.setups.pop(old)
                self.transition(old, setup, clock, "cancelled", "session_changed")
                self.windows.pop(old, None)
                self.setup_clocks.pop(old, None)
        source = datetime.fromisoformat(higher["source_5m_end"]) if higher["source_5m_end"] else None
        qualified = bool(all(higher["filters"].values()) and period and source
                         and period[0] + 5 * MINUTE <= source <= clock < source + 5 * MINUTE)
        window, previous = self.windows.get(identity), self.setups.get(identity)
        reason = None
        if previous:
            if period is None or previous["window"]["period_open"] != period[0].isoformat():
                reason = "session_changed"
            elif clock >= datetime.fromisoformat(previous["expires_at"]):
                reason = "expired"
            elif not qualified:
                reason = "higher_invalid"
            elif sign * ((bar.low if sign > 0 else bar.high) - previous["invalidation_level"]) < -tick * 1e-8:
                reason = "structure_broken"
            elif self.setup_clocks[identity] != bar.datetime.isoformat():
                reason = "nonadjacent"
            if reason:
                self.transition(identity, previous, clock, "cancelled", reason)
                cancellations.append(reason)
                self.setups.pop(identity)
                self.setup_clocks.pop(identity, None)
                previous = None
        if not qualified:
            self.windows.pop(identity, None)
            window = None
        elif window is None or window["period_open"] != period[0].isoformat():
            window = {"armed_at": clock.isoformat(), "expires_at": period[1].isoformat(),
                      "source_5m_end": higher["source_5m_end"], "period_open": period[0].isoformat()}
            self.windows[identity] = window
        else:
            # A fresh completed 5m candle reviews the environment. The arming
            # clock and the pattern's own expiry are retained.
            window["source_5m_end"] = higher["source_5m_end"]
        valid = bool(window and clock < period[1])
        low = {k: v for k, v in base["filters"].items() if k not in REMOVED}
        eligible = qualified and valid and all(low.values())
        confirmation, event, setup = None, None, None
        if previous:
            crossed = sign * (bar.close - previous["confirmation_level"]) > tick * 1e-8
            if crossed:
                confirmation = previous
                event = self.pattern_event(previous)
                self.transition(identity, previous, clock, "confirmed", "continuation_close",
                                execution_filters_passed=bool(eligible))
                self.setups.pop(identity)
                self.setup_clocks.pop(identity, None)
            else:
                self.transition(identity, previous, clock, "observed", "awaiting_confirmation")
                self.setup_clocks[identity] = clock.isoformat()
        past = self.features.past(bar.key, clock, max(11, rule["breakout_lookback_bars"] + 1))
        if eligible and not event and identity not in self.setups and past is not None:
            n = rule["breakout_lookback_bars"]
            before = past.iloc[-n-1:-1]
            adjacent = len(before) == n and all(
                t == clock - (n-i)*MINUTE for i, t in enumerate(before.index))
            armed = datetime.fromisoformat(window["armed_at"])
            touch = pullback_event(past, candidate["direction"], "pullback_" + rule["pullback_reference"],
                                   tick, self.data.cfg)
            pullback = bool("pullback" in rule["patterns"] and touch
                            and datetime.fromisoformat(touch["event"]) - MINUTE >= armed)
            boundary = float(before.high.max() if sign > 0 else before.low.min()) if adjacent else None
            breakout = bool("breakout" in rule["patterns"] and adjacent and bar.datetime >= armed
                            and sign * (bar.close - boundary) > tick * 1e-8)
            if breakout or pullback:
                # Recovery has priority in its own channel; it needs no second
                # expansion beyond the recovery candle's extreme.
                recovery = pullback and rule["pullback_confirmation"] == "recovery_close"
                kind = "confirmed_pullback" if recovery or not breakout else "confirmed_breakout"
                setup = {"kind": kind, "setup_end": clock.isoformat(), "setup_start": bar.datetime.isoformat(),
                         "expires_at": min(clock + rule["valid_minutes"]*MINUTE, period[1]).isoformat(),
                         "invalidation_level": bar.low if sign > 0 else bar.high,
                         "confirmation_level": bar.high if sign > 0 else bar.low,
                         "breakout_boundary": boundary, "reference_ends": [t.isoformat() for t in before.index],
                         "references": [int(rule["pullback_reference"][2:])] if kind == "confirmed_pullback" else [],
                         "window": dict(window), "higher": higher,
                         "touch_event": touch["event"] if pullback else None,
                         "dual_touch": touch["dual_touch"] if pullback else False,
                         "epsilon": touch["epsilon"] if pullback else 0}
                self.transition(identity, setup, clock, "formed", kind)
                if recovery:
                    confirmation, event = setup, self.pattern_event(setup)
                    # Deduplicate by touch, so several recovery closes cannot
                    # repeatedly consume one pullback after a fill cancellation.
                    event["event"] = setup["touch_event"]
                    self.transition(identity, setup, clock, "confirmed", "recovery_close",
                                    execution_filters_passed=True)
                else:
                    self.setups[identity] = setup
                    self.setup_clocks[identity] = clock.isoformat()
        confirmed = event is not None
        detail = {"qualified": qualified, "window": dict(window) if window else None, "higher": higher,
                  "confirmation": confirmation, "setup": setup,
                  "confirmation_time": clock.isoformat() if confirmed else None,
                  "cancellations": cancellations, "pending_setup": self.setups.get(identity),
                  "state_policy": "setup_lifetime"}
        if setup or previous or confirmation or cancellations:
            self.journal.append({"trigger": False, "time": clock.isoformat(), "contract": bar.key,
                                 "direction": candidate["direction"], "detail": detail, "confirmed": confirmed})
        filters = low | {"higher_trend_quality": qualified, "confirmation_window": valid,
                         "price_pattern_confirmed": confirmed}
        supplement = {"filters": filters, "all_pass": all(filters.values()),
                      "snapshot": dict(base["snapshot"]) | {"price_confirmation": detail},
                      "rejections": [k for k, v in filters.items() if not v], "pullback": event,
                      "exit_flags": base["exit_flags"]}
        if self.data.cfg["strategy"].get("structure_protection"):
            context = structure_context(self.data, self.features, bar, candidate["direction"])
            base["snapshot"]["structure_stop"] = context
            supplement["snapshot"]["structure_stop"] = context
        return dict(base) | {"_confirmed_channels": {"legacy": base, "confirmed": supplement},
                             "_confirmation_time": clock.isoformat()}

    @staticmethod
    def pattern_event(setup):
        return {"event": setup["setup_end"], "kind": setup["kind"],
                "references": setup.get("references", [10] if setup["kind"] == "confirmed_pullback" else []),
                "dual_touch": setup.get("dual_touch", False), "epsilon": setup.get("epsilon", 0),
                "touch_event": setup.get("touch_event")}

    def higher(self, bar, candidate, base):
        five = self.features.latest(bar.key, 5, bar.end)
        fifteen = self.features.latest(bar.key, 15, bar.end)
        sign, s = (1 if candidate["direction"] == "LONG" else -1), self.data.cfg["strategy"]
        cache_key = bar.key, candidate["direction"]
        identity = (five.name if five is not None else None,
                    fifteen.name if fifteen is not None else None)
        if cache_key not in self.quality_cache or self.quality_cache[cache_key][0] != identity:
            past = self.features.past_period(bar.key, 5, bar.end, 11)
            meta = self.data.metadata.get(bar.key, bar.trading_day)
            quality, details = trend_quality(past, candidate["direction"], meta["tick_size"],
                                             float(five.previous_atr) if five is not None else None, s["trend_quality"])
            efficiency = sign * float(five.efficiency) if five is not None and finite(five.efficiency) else None
            quality["efficiency"] = efficiency is not None and efficiency >= s["entry_confirmation"]["higher_efficiency_min"]
            self.quality_cache[cache_key] = identity, quality, details, efficiency
        _, quality, details, efficiency = self.quality_cache[cache_key]
        wanted = ("warmup_higher", "current_session_15m", "trend_15m", "trend_5m",
                  "slope_5m_ready", "slope_5m_minimum", "slope_5m_maximum")
        checks = {k: base["filters"][k] for k in wanted} | quality
        return {"filters": checks, "trend_quality": details, "efficiency": efficiency,
                "source_5m_end": five.name.isoformat() if five is not None else None,
                "source_15m_end": fifteen.name.isoformat() if fifteen is not None else None}

    def evaluate(self, bar, candidate, state_allows=True, before_cutoff=True):
        base = self.baseline(bar, candidate, state_allows, before_cutoff)
        s = self.data.cfg["strategy"]
        if not s.get("entry_confirmation"):
            if s.get("structure_protection"):
                base["snapshot"]["structure_stop"] = structure_context(self.data, self.features, bar, candidate["direction"])
            return base
        higher = self.higher(bar, candidate, base)
        meta = self.data.metadata.get(bar.key, bar.trading_day)
        period = next(((a, b) for a, b in self.data.calendar.periods(bar.trading_day, meta)
                       if a <= bar.datetime < b), None)
        if s["entry_confirmation"].get("state_policy") == "setup_lifetime":
            return self.lifetime(bar, candidate, base, higher, period)
        identity = bar.trading_day, bar.key, candidate["direction"]
        window = self.windows.get(identity)
        source = datetime.fromisoformat(higher["source_5m_end"]) if higher["source_5m_end"] else None
        qualified = all(higher["filters"].values()) and period is not None and source is not None and source >= period[0] + 5 * MINUTE
        if not qualified:
            self.windows.pop(identity, None)
            old = self.setups.pop(identity, None)
            if old:
                self.transition(identity, old, bar.end, "cancelled", "higher_invalid")
            window = None
        elif window is None or window["source_5m_end"] != higher["source_5m_end"] or window["period_open"] != period[0].isoformat():
            window = {"armed_at": bar.end.isoformat(), "expires_at": min(source + 5 * MINUTE, period[1]).isoformat(),
                      "source_5m_end": higher["source_5m_end"], "period_open": period[0].isoformat()}
            self.windows[identity] = window
            old = self.setups.pop(identity, None)
            if old:
                self.transition(identity, old, bar.end, "cancelled", "five_minute_boundary")
        valid = bool(window and window["armed_at"] <= bar.end.isoformat() < window["expires_at"])
        low_filters = {k: v for k, v in base["filters"].items() if k not in REMOVED}
        eligible = valid and qualified and all(low_filters.values())
        past = self.features.past(bar.key, bar.end, 11)
        sign = 1 if candidate["direction"] == "LONG" else -1
        previous_setup = self.setups.pop(identity, None)
        confirmed = bool(eligible and previous_setup and previous_setup["setup_end"] == bar.datetime.isoformat()
                         and sign * (bar.close - previous_setup["confirmation_level"]) > meta["tick_size"] * 1e-8)
        event = None
        if confirmed:
            event = {"event": previous_setup["setup_end"], "kind": previous_setup["kind"],
                     "references": [10] if previous_setup["kind"] == "confirmed_pullback" else [],
                     "dual_touch": previous_setup.get("dual_touch", False),
                     "epsilon": previous_setup.get("epsilon", 0), "touch_event": previous_setup.get("touch_event")}
        setup = None
        if eligible and past is not None and len(past) >= 4 and not confirmed:
            current, before = past.iloc[-1], past.iloc[-3:-1]
            adjacent = (past.index[-1] == bar.end and past.index[-2] == bar.datetime
                        and past.index[-3] == bar.datetime - MINUTE)
            armed = datetime.fromisoformat(window["armed_at"])
            touch = pullback_event(past, candidate["direction"], "pullback_ma10", meta["tick_size"], self.data.cfg)
            pullback = bool(touch and datetime.fromisoformat(touch["event"]) - MINUTE >= armed)
            boundary = float(before.high.max() if sign > 0 else before.low.min())
            breakout = bool(adjacent and bar.datetime >= armed
                            and sign * (current.close - boundary) > meta["tick_size"] * 1e-8)
            if adjacent and (breakout or pullback):
                setup = {"kind": "confirmed_breakout" if breakout else "confirmed_pullback",
                         "setup_end": bar.end.isoformat(), "setup_start": bar.datetime.isoformat(),
                         "confirmation_level": bar.high if sign > 0 else bar.low,
                         "breakout_boundary": boundary, "reference_ends": [t.isoformat() for t in past.index[-3:-1]],
                         "window": dict(window), "higher": higher,
                         "touch_event": touch["event"] if pullback else None,
                         "dual_touch": touch["dual_touch"] if pullback else False,
                         "epsilon": touch["epsilon"] if pullback else 0}
                self.setups[identity] = setup
                self.transition(identity, setup, bar.end, "formed", setup["kind"])
        if previous_setup:
            reason = ("next_close" if confirmed else "expired" if not valid else
                      "nonadjacent" if previous_setup["setup_end"] != bar.datetime.isoformat() else
                      "execution_filters" if not eligible else "confirmation_failed")
            self.transition(identity, previous_setup, bar.end, "confirmed" if confirmed else "cancelled", reason)
        detail = {"qualified": bool(qualified), "window": window, "higher": higher,
                  "confirmation": previous_setup if confirmed else None, "setup": setup,
                  "confirmation_time": bar.end.isoformat() if confirmed else None}
        if setup or previous_setup:
            self.journal.append({"trigger": False, "time": bar.end.isoformat(), "contract": bar.key,
                                 "direction": candidate["direction"], "detail": detail, "confirmed": confirmed})
        filters = low_filters | {"higher_trend_quality": bool(qualified), "confirmation_window": valid,
                                 "price_pattern_confirmed": confirmed}
        supplement = {"filters": filters, "all_pass": all(filters.values()),
                      "snapshot": dict(base["snapshot"]) | {"price_confirmation": detail},
                      "rejections": [k for k, v in filters.items() if not v], "pullback": event,
                      "exit_flags": base["exit_flags"]}
        if s.get("structure_protection"):
            context = structure_context(self.data, self.features, bar, candidate["direction"])
            base["snapshot"]["structure_stop"] = context
            supplement["snapshot"]["structure_stop"] = context
        return dict(base) | {"_confirmed_channels": {"legacy": base, "confirmed": supplement},
                             "_confirmation_time": bar.end.isoformat()}


class StructureBacktest(PortfolioBacktest):
    extra_journals = ("confirmation_setups", "confirmed_channels", "confirmation_events", "opportunity_diagnostics")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.confirmation_setups, self.confirmed_channels = [], []
        self.confirmation_events, self.opportunity_diagnostics = [], []

    def entry_protection(self, signal, meta, roundtrip_fees, minimum_stop_ticks=0, price=None):
        result = super().entry_protection(signal, meta, roundtrip_fees, minimum_stop_ticks, price)
        rule = self.cfg["strategy"].get("structure_protection")
        if not rule:
            return self.profit_protection(signal, meta, roundtrip_fees, result)
        context = signal["snapshot"].get("structure_stop", {})
        if not context.get("ready"):
            return result | {"accepted": False, "rejections": ["structure_history_missing"]}
        sign = 1 if signal["direction"] == "LONG" else -1
        tick = meta["tick_size"]
        anchor = context["extreme"] - sign * rule["buffer_ticks"] * tick
        price = float(price)
        distance = sign * (price - anchor)
        ticks = math.ceil(distance / tick - 1e-9)
        stop = max(result["stop_loss_ticks"], ticks)
        original = self.cfg["strategy"]["fixed_ticks"][meta["product"]]
        rejected = (["structure_already_broken"] if distance <= tick * 1e-8 else
                    ["structure_distance_exceeds_atr_cap"] if stop * tick > rule["max_stop_atr"] * context["atr_previous"] + tick * 1e-8 else [])
        result = result | {"accepted": not rejected, "rejections": rejected,
                         "stop_loss_ticks": stop,
                         "take_profit_ticks": math.ceil(stop * original["take_profit_ticks"] / original["stop_loss_ticks"] - 1e-9),
                         "structure_anchor": anchor, "structure_distance_ticks": ticks,
                         "structure_atr_previous": context["atr_previous"], "structure_sources": context["sources"],
                         "structure_source_end": context["source_end"], "max_stop_atr": rule["max_stop_atr"]}
        return self.profit_protection(signal, meta, roundtrip_fees, result)

    def profit_protection(self, signal, meta, roundtrip_fees, result):
        rule = self.cfg["strategy"].get("profit_protection")
        if not rule:
            return result
        reference = signal.get("profit_protection_reference")
        if reference is None:
            # Exclude both the structural widening and the reservation floor.
            # Signal-time distances remain fixed when the next open is checked.
            base = super().entry_protection(signal, meta, roundtrip_fees, 0)
            reference = {"signal_time": signal["time"], "stop_ticks": base["stop_loss_ticks"],
                         "target_ticks": base["take_profit_ticks"], "tick_size": meta["tick_size"]}
            signal["profit_protection_reference"] = reference
        if reference["tick_size"] != meta["tick_size"]:
            return result | {"accepted": False, "rejections": ["profit_reference_tick_changed"]}
        target = math.ceil(reference["target_ticks"] * rule["target_multiple"] - 1e-9)
        return result | {"take_profit_ticks": target, "profit_protection_reference": dict(reference),
                         "breakeven_activation_distance": reference["stop_ticks"] * meta["tick_size"] * rule["breakeven_multiple"],
                         "trailing_atr_multiple": rule["trailing_atr_multiple"],
                         "profit_protection_basis": rule["basis"]}

    def diagnose_opportunity(self, bar, candidate, evaluation):
        """Record price space and account feasibility without changing gates."""
        channels = evaluation.get("_confirmed_channels", {"legacy": evaluation})
        clock, s = bar.end, self.cfg["strategy"]
        meta, execution_rejections = self.parameters.resolve(bar.key, clock)
        for name, channel in channels.items():
            flags = channel["filters"]
            market = all(v for k, v in flags.items() if k not in {"cost", "stop_reentry", "state", "entry_time"})
            event = channel.get("pullback")
            # Include a confirmed shape even when a price/state/cost gate fails.
            shape = name == "confirmed" and event is not None
            if not market and not shape:
                continue
            record = {"trigger": False, "time": clock.isoformat(), "date": bar.trading_day,
                      "contract": bar.key, "direction": candidate["direction"], "rank": candidate["rank"],
                      "channel": event["kind"] if event else name,
                      "shape_id": event["event"] if event else clock.isoformat(),
                      "market_qualified": market, "market_rejections": [k for k, v in flags.items()
                          if not v and k not in {"cost", "stop_reentry", "state", "entry_time"}],
                      "execution_rejections": execution_rejections,
                      "state_allows": flags.get("state", False), "before_cutoff": flags.get("entry_time", False),
                      "price": bar.close, "cost_pass": evaluation["filters"].get("cost", False)}
            if meta is not None and not execution_rejections:
                fees = fee(meta, bar.trading_day, "open", bar.close, 1) + fee(meta, bar.trading_day, "close_today", bar.close, 1)
                signal = {"snapshot": channel["snapshot"], "direction": candidate["direction"],
                          "time": clock.isoformat()}
                base = super().entry_protection(signal, meta, fees, price=bar.close)
                protection = self.entry_protection(signal, meta, fees, price=bar.close)
                distance = fees / meta["value_per_price"] + 2*s["slippage_ticks"]*meta["tick_size"]
                five = self.features.latest(bar.key, 5, clock)
                atr5 = float(five.previous_atr) if five is not None and finite(five.previous_atr) and five.previous_atr > 0 else None
                risk_distance = protection["stop_loss_ticks"] * meta["tick_size"]
                space = base["take_profit_ticks"] * meta["tick_size"]
                atr1 = channel["snapshot"].get("atr_previous")
                def ratio(denominator, cost_distance=distance):
                    return cost_distance/denominator if finite(denominator) and denominator > 0 else None
                r, minimum = self.cfg["risk"], meta.get("min_open_lots", 1)
                per_risk = risk_distance*meta["value_per_price"] + r["cost_buffer_multiple"]*distance*meta["value_per_price"]
                per_margin = bar.close*meta["value_per_price"]*meta["margin_rate"]
                fraction = r["group_fractions"].get(meta["group"], 0)
                shares = {"single_trade_risk": r["trade_risk_fraction"], "portfolio_risk": r["portfolio_risk_fraction"],
                          "group_risk": r["portfolio_risk_fraction"]*fraction,
                          "margin": r["margin_fraction"], "group_margin": r["margin_fraction"]*fraction}
                needs = {k: minimum*(per_margin if "margin" in k else per_risk)/v if v > 0 else None
                         for k, v in shares.items()}
                required = max(needs.values()) if all(v is not None for v in needs.values()) else None
                quantity, _, _, reasons = self.allocator.allocate(meta, bar.close, bar.trading_day, self.states,
                    self.equity_value(), remaining_open_lots=self.remaining_open_lots(meta, bar.trading_day),
                    stop_loss_ticks=protection["stop_loss_ticks"])
                record.update(roundtrip_cost_distance=distance, cost_to_atr_1m=ratio(atr1), cost_to_atr_5m=ratio(atr5),
                              cost_to_predefined_space=ratio(space), cost_to_initial_risk=ratio(risk_distance),
                              atr_5m_source_end=five.name.isoformat() if five is not None else None,
                              predefined_space=space, space_basis="original_training_target_ratio_and_signal_base_floors",
                              initial_risk_distance=risk_distance, structure_rejections=protection.get("rejections", []),
                              minimum_open_lots=minimum, risk_per_lot=per_risk, margin_per_lot=per_margin,
                              required_by_limit=needs, minimum_required_capital_and_equity=required,
                              risk_requires_larger_initial_capital=any(v is not None and v > r["initial_capital"]
                                  for k, v in needs.items() if "risk" in k),
                              current_quantity_capacity=quantity, account_rejections=reasons,
                              affordability_basis="empty_portfolio_lower_bound; current_capacity_includes_reservations_and_daily_limits")
            self.opportunity_diagnostics.append(record)

    def entry_trigger(self, key, evaluation):
        if observation := evaluation.pop("_observation", None):
            self.diagnose_opportunity(*observation, evaluation)
        if not self.cfg["strategy"].get("entry_confirmation"):
            return super().entry_trigger(key, evaluation)
        channels = evaluation.pop("_confirmed_channels")
        clock = evaluation.pop("_confirmation_time")
        common = {k: v for k, v in evaluation["filters"].items() if k in {"cost", "stop_reentry"}}
        cost = evaluation.get("cost_check")
        state = self.state(key)
        previous = state.previous_pass
        passes = {name: all(row["filters"].values()) and all(common.values()) for name, row in channels.items()}
        event = channels["confirmed"]["pullback"]
        consumed = bool(event and event["event"] in state.consumed)
        triggers = {"legacy": passes["legacy"] and not previous,
                    "confirmed": passes["confirmed"] and bool(event) and not consumed}
        chosen = ("legacy" if triggers["legacy"] else "confirmed" if triggers["confirmed"] else
                  "legacy" if passes["legacy"] else "confirmed" if passes["confirmed"] else "legacy")
        actual = channels[chosen]
        label = "legacy" if chosen == "legacy" else event["kind"]
        flags = actual["filters"] | common
        evaluation.clear()
        evaluation.update(actual, filters=flags, all_pass=all(flags.values()),
                          rejections=[k for k, v in flags.items() if not v],
                          snapshot=dict(actual["snapshot"]) | {"entry_channel": label})
        if cost is not None:
            evaluation["cost_check"] = cost
        state.previous_pass = passes["legacy"]
        if any(passes.values()) or any(triggers.values()):
            self.confirmed_channels.append({"trigger": False, "time": clock, "contract": key,
                                            "chosen": chosen, "entry_channel": label, "passes": passes,
                                            "triggers": triggers, "previous_legacy_pass": previous,
                                            "consumed": consumed, "event": event, "common": common,
                                            "channel_filters": {k: v["filters"] for k, v in channels.items()}})
        return triggers[chosen]

    def opportunity_priority(self, opportunity):
        original = super().opportunity_priority(opportunity)
        if not self.cfg["strategy"].get("entry_confirmation"):
            return original
        channel = opportunity[0]["snapshot"]["entry_channel"]
        return ({"legacy": 0, "confirmed_breakout": 1, "confirmed_pullback": 2}[channel], *original)

    def run(self, *args, **kwargs):
        if isinstance(getattr(self.logic, "trend", None), ConfirmedLogic):
            self.logic.trend.journal = self.confirmation_setups
            self.logic.trend.events = self.confirmation_events
        result = super().run(*args, **kwargs)
        if isinstance(getattr(self.logic, "trend", None), ConfirmedLogic):
            self.logic.trend.finish(at(result["end"], "16:00"))
        return result | {"confirmation_setups": self.confirmation_setups,
                         "confirmed_channels": self.confirmed_channels,
                         "confirmation_events": self.confirmation_events,
                         "opportunity_diagnostics": self.opportunity_diagnostics}
