"""Ordered, optional research rules; all decisions use completed observations."""

import math
from collections import defaultdict
from datetime import datetime

from .calendar import MINUTE
from .execution import PortfolioBacktest, fee
from .refinements import scaled_protection, trend_quality
from .signals import finite, pullback_event


class OpportunityBacktest(PortfolioBacktest):
    extra_journals = ("candidate_selection", "exit_confirmation")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidate_selection, self.exit_confirmation = [], []
        self.ma40_history = {}

    def assess_capacity(self, candidate, bar, cutoff):
        """Unknown inputs retain rank slots; only known zero capacity is skipped."""
        if bar is None or bar.end != cutoff:
            return {"assessed": False, "reason": "current_completed_bar_missing"}
        meta, reasons = self.parameters.resolve(candidate["contract"], cutoff)
        if meta is None or reasons:
            return {"assessed": False, "reason": "execution_inputs_unavailable"}
        one = self.features.latest(candidate["contract"], 1, cutoff)
        atr = one.previous_atr if one is not None else None
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return {"assessed": False, "reason": "causal_atr_unavailable"}
        price, day, s = bar.close, bar.trading_day, self.cfg["strategy"]
        fees = fee(meta, day, "open", price, 1) + fee(meta, day, "close_today", price, 1)
        signal = {"snapshot": {"atr_previous": float(atr)}, "time": cutoff.isoformat()}
        protection = scaled_protection(signal, meta, s, fees)
        quantity, risk, margin, reasons = self.allocator.allocate(
            meta, price, day, self.states, self.equity_value(),
            remaining_open_lots=self.remaining_open_lots(meta, day),
            stop_loss_ticks=protection["stop_loss_ticks"],
        )
        return {"assessed": True, "quote_time": cutoff.isoformat(), "price": price,
                "atr_previous": float(atr), "quantity": quantity,
                "minimum_open_lots": meta.get("min_open_lots", 1),
                "planned_risk": risk, "margin": margin,
                "rejections": reasons, "stop_loss_ticks": protection["stop_loss_ticks"],
                "roundtrip_fees_per_lot": fees,
                "tick_size": meta["tick_size"], "value_per_price": meta["value_per_price"],
                "margin_rate": meta["margin_rate"],
                "daily_open_remaining": self.remaining_open_lots(meta, day)}

    def update_candidates(self, candidates, current, day, cutoff):
        if not self.cfg["strategy"].get("candidate_replacement"):
            return
        cohorts = defaultdict(list)
        for candidate in candidates.values():
            cohorts[(candidate["group"], candidate["direction"])].append(candidate)
        selected = set()
        for (group, direction), cohort in sorted(cohorts.items()):
            ordered = sorted(cohort, key=lambda c: c["rank"])
            retained = [c for c in ordered if self.state(c["contract"]).position
                        or self.state(c["contract"]).name == "ENTRY_PENDING"]
            chosen = {c["contract"] for c in retained}
            checks = [{"contract": c["contract"], "rank": c["rank"],
                       "action": "retain_held_or_reserved"} for c in retained]
            for c in ordered:
                if len(chosen) >= self.cfg["strategy"]["k"]:
                    break
                key = c["contract"]
                if key in chosen:
                    continue
                assessment = self.assess_capacity(c, current.get(key), cutoff)
                skip = assessment["assessed"] and assessment["quantity"] == 0
                checks.append({"contract": key, "rank": c["rank"],
                               "action": "skip_zero_capacity" if skip else "select",
                               **assessment})
                if not skip:
                    chosen.add(key)
            selected.update(chosen)
            if any(c["action"] == "skip_zero_capacity" for c in checks) or any(c["rank"] > 2 for c in checks if c["contract"] in chosen):
                risk, margin, slots, usage = self.allocator.usage(self.states)
                self.candidate_selection.append({
                    "trigger": False, "time": cutoff.isoformat(), "date": day,
                    "group": group, "direction": direction, "selected": sorted(chosen),
                    "checks": checks, "equity": self.equity_value(), "risk_used": risk,
                    "margin_used": margin, "slots_used": slots,
                    "group_usage": usage.get(group, {"risk": 0, "margin": 0}),
                })
        for key, candidate in list(candidates.items()):
            passing = key in selected
            if passing != candidate["selected"]:
                self.state(key).previous_pass = False
            # The stored daily rank records keep their original Top-K selection.
            candidates[key] = {**candidate, "selected": passing}

    def position_exit_flags(self, key, bar, one, past, direction):
        flags = super().position_exit_flags(key, bar, one, past, direction)
        if self.cfg["strategy"].get("ma40_exit_confirmation_bars", 1) == 1:
            return flags
        position = self.state(key).position
        identity = position.opened.isoformat()
        previous = self.ma40_history.get(key)
        crossing = "ma40_cross" in flags
        consecutive = bool(previous and previous["entry_time"] == identity
                           and previous["end"] == bar.datetime and previous["crossing"])
        confirmed = crossing and consecutive
        self.ma40_history[key] = {"entry_time": identity, "end": bar.end, "crossing": crossing}
        if crossing:
            self.exit_confirmation.append({
                "trigger": False, "time": bar.end.isoformat(), "contract": key,
                "entry_time": identity, "close": float(one.close), "ma40": float(one.ma40),
                "previous_end": previous["end"].isoformat() if previous else None,
                "consecutive": consecutive, "confirmed": confirmed,
            })
        return flags if confirmed else [f for f in flags if f != "ma40_cross"]

    def run(self, *args, **kwargs):
        result = super().run(*args, **kwargs)
        result.update(candidate_selection=self.candidate_selection,
                      exit_confirmation=self.exit_confirmation)
        return result


class TrendWindowLogic:
    """Higher-period quality arms a short window; a completed MA10 pullback triggers."""

    def __init__(self, original, baseline):
        self.original, self.baseline = original, baseline
        self.data, self.features = original.data, original.features
        self.windows, self.quality_cache = {}, {}

    def higher_quality(self, bar, candidate, base):
        key, direction, cutoff = bar.key, candidate["direction"], bar.end
        sign, s = (1 if direction == "LONG" else -1), self.data.cfg["strategy"]
        five, fifteen = self.features.latest(key, 5, cutoff), self.features.latest(key, 15, cutoff)
        identity = (key, direction, five.name if five is not None else None,
                    fifteen.name if fifteen is not None else None)
        cache_identity = key, direction
        if cache_identity not in self.quality_cache or self.quality_cache[cache_identity][0] != identity:
            past = self.features.past_period(key, 5, cutoff, 11)
            meta = self.data.metadata.get(key, bar.trading_day)
            quality, diagnostic = trend_quality(
                past, direction, meta.get("tick_size"),
                float(five.previous_atr) if five is not None else None, s["trend_quality"],
            )
            value = sign * float(five.efficiency) if five is not None and finite(five.efficiency) else None
            quality["efficiency"] = value is not None and value >= s["trend_entry"]["efficiency_min"]
            self.quality_cache[cache_identity] = (identity, quality, diagnostic, value)
        _, quality, diagnostic, efficiency = self.quality_cache[cache_identity]
        wanted = ["warmup_higher", "current_session_15m", "trend_15m", "trend_5m",
                  "slope_5m_ready", "slope_5m_minimum", "slope_5m_maximum"]
        flags = {k: base["filters"][k] for k in wanted} | quality
        source = {"quality_timeframe": "5m", "efficiency": efficiency,
                  "efficiency_min": s["trend_entry"]["efficiency_min"],
                  "trend_quality": diagnostic,
                  "source_5m_end": five.name.isoformat() if five is not None else None,
                  "source_15m_end": fifteen.name.isoformat() if fifteen is not None else None,
                  "filters": flags}
        return all(flags.values()), source

    def evaluate(self, bar, candidate, state_allows=True, before_cutoff=True):
        base = self.baseline(bar, candidate, state_allows, before_cutoff)
        passing, source = self.higher_quality(bar, candidate, base)
        meta = self.data.metadata.get(bar.key, bar.trading_day)
        period = next((a for a, b in self.data.calendar.periods(bar.trading_day, meta)
                       if a <= bar.datetime < b), None)
        identity = (bar.trading_day, bar.key, candidate["direction"])
        previous = self.windows.get(identity)
        completed = source["source_5m_end"]
        if not passing or period is None:
            self.windows.pop(identity, None)
            window = None
        elif previous is None or previous["source_5m_end"] != completed or previous["period"] != period:
            arm = datetime.fromisoformat(completed)
            window = {"armed_at": arm, "expires_at": arm + 5 * MINUTE,
                      "source_5m_end": completed, "period": period}
            self.windows[identity] = window
        else:
            window = previous
        valid = bool(window and window["armed_at"] <= bar.end < window["expires_at"]
                     and window["period"] == period and window["armed_at"] >= period)
        past = self.features.past(bar.key, bar.end, 11)
        touch = pullback_event(past, candidate["direction"], "pullback_ma10", meta.get("tick_size"), self.data.cfg)
        after_arm = bool(valid and touch and datetime.fromisoformat(touch["event"]) >= window["armed_at"])
        removed = {"efficiency", "trend_activity", "trend_displacement",
                   "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"}
        filters = {k: v for k, v in base["filters"].items() if k not in removed}
        filters.update(higher_trend_quality=passing, trend_window_valid=valid,
                       pullback_after_armed=after_arm)
        snapshot = dict(base["snapshot"])
        snapshot["trend_entry"] = source | {
            "armed_at": window["armed_at"].isoformat() if window else None,
            "expires_at": window["expires_at"].isoformat() if window else None,
            "period_open": period.isoformat() if period else None,
            "removed_low_period_filters": {k: base["filters"][k] for k in sorted(removed)},
        }
        return {"filters": filters, "snapshot": snapshot,
                "rejections": [k for k, v in filters.items() if not v],
                "all_pass": all(filters.values()), "exit_flags": base["exit_flags"],
                "pullback": touch}
