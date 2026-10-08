"""Optional confirmed price patterns and frozen five-minute structural risk."""

import math
from datetime import datetime

from .calendar import MINUTE
from .execution import PortfolioBacktest
from .refinements import trend_quality
from .signals import finite, pullback_event

REMOVED = {"efficiency", "trend_activity", "trend_displacement",
           "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"}


def structure_context(data, features, bar, direction):
    meta = data.metadata.get(bar.key, bar.trading_day)
    period = next(((a, b) for a, b in data.calendar.periods(bar.trading_day, meta)
                   if a <= bar.datetime < b), None)
    past = features.past_period(bar.key, 5, bar.end, 3)
    result = {"signal_time": bar.end.isoformat(), "period_open": period[0].isoformat() if period else None,
              "ready": False, "sources": []}
    if period is None or past is None or len(past) != 3:
        return result
    result["sources"] = [{"end": t.isoformat(), "low": float(row.low), "high": float(row.high)}
                         for t, row in past.iterrows()]
    if any(t - 5 * MINUTE < period[0] or t > bar.end for t in past.index):
        return result
    if any(b - a != 5 * MINUTE for a, b in zip(past.index[:-1], past.index[1:], strict=True)):
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
        identity = bar.trading_day, bar.key, candidate["direction"]
        window = self.windows.get(identity)
        source = datetime.fromisoformat(higher["source_5m_end"]) if higher["source_5m_end"] else None
        qualified = all(higher["filters"].values()) and period is not None and source is not None and source >= period[0] + 5 * MINUTE
        if not qualified:
            self.windows.pop(identity, None)
            self.setups.pop(identity, None)
            window = None
        elif window is None or window["source_5m_end"] != higher["source_5m_end"] or window["period_open"] != period[0].isoformat():
            window = {"armed_at": bar.end.isoformat(), "expires_at": min(source + 5 * MINUTE, period[1]).isoformat(),
                      "source_5m_end": higher["source_5m_end"], "period_open": period[0].isoformat()}
            self.windows[identity] = window
            self.setups.pop(identity, None)
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
    extra_journals = ("confirmation_setups", "confirmed_channels")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.confirmation_setups, self.confirmed_channels = [], []

    def entry_protection(self, signal, meta, roundtrip_fees, minimum_stop_ticks=0, price=None):
        result = super().entry_protection(signal, meta, roundtrip_fees, minimum_stop_ticks, price)
        rule = self.cfg["strategy"].get("structure_protection")
        if not rule:
            return result
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
        return result | {"accepted": not rejected, "rejections": rejected,
                         "stop_loss_ticks": stop,
                         "take_profit_ticks": math.ceil(stop * original["take_profit_ticks"] / original["stop_loss_ticks"] - 1e-9),
                         "structure_anchor": anchor, "structure_distance_ticks": ticks,
                         "structure_atr_previous": context["atr_previous"], "structure_sources": context["sources"],
                         "structure_source_end": context["source_end"], "max_stop_atr": rule["max_stop_atr"]}

    def entry_trigger(self, key, evaluation):
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
        result = super().run(*args, **kwargs)
        return result | {"confirmation_setups": self.confirmation_setups,
                         "confirmed_channels": self.confirmed_channels}
