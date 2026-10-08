"""Shared causal signals: offline execution and optional vn.py bridge call this code."""

import hashlib
import math
from bisect import bisect_right
from pathlib import Path

import numpy as np
import pandas as pd
import talib

from .calendar import MINUTE
from .config import digest
from .refinements import slope_band, trend_quality, volume_weakness


def finite(value):
    return value is not None and math.isfinite(float(value))


def feature_frame(rows, atr_period):
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame.index = pd.DatetimeIndex(frame.pop("end"))
    for period in (10, 20, 40):
        frame[f"ma{period}"] = frame.close.rolling(period, min_periods=period).mean()
    close, high, low = (
        frame[k].to_numpy(dtype=float) for k in ("close", "high", "low")
    )
    frame["atr"] = talib.ATR(high, low, close, timeperiod=atr_period)
    frame["previous_atr"] = frame.atr.shift(1)
    tr = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - frame.close.shift(1)).abs(),
            (frame.low - frame.close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["shock"] = (
        (tr / frame.previous_atr.replace(0, np.nan)).rolling(5, min_periods=5).max()
    )
    path = frame.close.diff().abs().rolling(10, min_periods=10).sum()
    frame["efficiency"] = (frame.close - frame.close.shift(10)) / path.replace(
        0, np.nan
    )
    frame["volume_baseline"] = (
        frame.volume.shift(1).rolling(20, min_periods=20).median()
    )
    frame["vr"] = frame.volume / frame.volume_baseline.where(frame.volume_baseline > 0)
    frame["ma20_slope3"] = frame.ma20 - frame.ma20.shift(3)
    return frame


class Features:
    def __init__(self, dataset, cache=None):
        self.data = dataset
        self.frames = {}
        s = dataset.cfg["strategy"]
        self.cache_key = digest(
            {
                "algorithm": "causal-sma-talib-wilder-complete-session-v1",
                "data": dataset.fingerprint,
                "source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "talib": talib.__version__,
                "atr": s["atr_period"],
                "night": s["include_night_indicators"],
                "calendar": dataset.cfg["calendar"],
                "metadata": dataset.cfg["metadata"],
            }
        )
        target = Path(cache) / (self.cache_key + ".jsonl.gz") if cache else None
        if target and target.exists():
            from .feature_cache import read_frames

            for key, minutes, records in read_frames(target, self.cache_key):
                frame = pd.DataFrame(records)
                if not frame.empty:
                    frame.index = pd.DatetimeIndex(frame.pop("end"))
                    for column in frame.columns:
                        if column not in {"datetime", "day", "period"}:
                            frame[column] = frame[column].astype(float)
                self.frames[(key, int(minutes))] = frame
            return
        for key, source in sorted(dataset.by_contract.items()):
            rows = []
            for b in source:
                meta = dataset.metadata.get(key, b.trading_day)
                period = dataset.calendar.locate(
                    b.datetime, b.trading_day, meta, night=s["include_night_indicators"]
                )
                if period:
                    rows.append(
                        {
                            "end": b.end.isoformat(),
                            "datetime": b.datetime.isoformat(),
                            "day": b.trading_day,
                            "period": period[0].isoformat(),
                            "open": b.open,
                            "high": b.high,
                            "low": b.low,
                            "close": b.close,
                            "volume": b.volume,
                        }
                    )
            self.frames[(key, 1)] = feature_frame(rows, s["atr_period"])
            for minutes in (5, 15):
                buckets = {}
                for row in rows:
                    start, dt = (
                        pd.Timestamp(row["period"]),
                        pd.Timestamp(row["datetime"]),
                    )
                    block = int((dt - start).total_seconds() // (60 * minutes))
                    buckets.setdefault((row["day"], row["period"], block), []).append(
                        row
                    )
                complete = []
                for (_, opening, block), bucket in sorted(buckets.items()):
                    start = pd.Timestamp(opening) + pd.Timedelta(
                        minutes=block * minutes
                    )
                    if len(bucket) != minutes or any(
                        pd.Timestamp(r["datetime"]) != start + i * MINUTE
                        for i, r in enumerate(bucket)
                    ):
                        continue
                    complete.append(
                        {
                            "end": (start + minutes * MINUTE).isoformat(),
                            "day": bucket[-1]["day"],
                            "period": opening,
                            "open": bucket[0]["open"],
                            "close": bucket[-1]["close"],
                            "high": max(r["high"] for r in bucket),
                            "low": min(r["low"] for r in bucket),
                            "volume": sum(r["volume"] for r in bucket),
                        }
                    )
                self.frames[(key, minutes)] = feature_frame(complete, s["atr_period"])
        if target and not target.exists():
            from .feature_cache import write_frames
            from .storage import SpaceBudget

            policy = dataset.cfg.get("storage", {}).get("budget")
            write_frames(
                target,
                self.cache_key,
                self.frames,
                SpaceBudget(policy) if policy else None,
            )

    def latest(self, key, minutes, cutoff):
        frame = self.frames.get((key, minutes))
        if frame is None or frame.empty:
            return None
        index = frame.index.searchsorted(pd.Timestamp(cutoff), side="right") - 1
        return frame.iloc[index] if index >= 0 else None

    def past(self, key, cutoff, count):
        frame = self.frames.get((key, 1))
        if frame is None or frame.empty:
            return frame
        index = frame.index.searchsorted(pd.Timestamp(cutoff), side="right")
        return frame.iloc[max(0, index - count) : index]


    def past_period(self, key, minutes, cutoff, count):
        """Read completed bars of a chosen period without changing legacy accessors."""
        frame = self.frames.get((key, minutes))
        if frame is None or frame.empty:
            return frame
        index = frame.index.searchsorted(pd.Timestamp(cutoff), side="right")
        return frame.iloc[max(0, index - count) : index]


def rank_candidates(dataset, day, pool, cutoff, k):
    ready, excluded = [], []
    for entry in pool:
        meta, key = entry["meta"], entry["contract"]
        opening, _ = dataset.calendar.bounds(day, meta)
        ranking_time = opening + 8 * MINUTE
        if cutoff < ranking_time:
            continue
        rows = dataset.by_day.get((day, key), {})
        observation = [rows.get(opening + i * MINUTE) for i in range(8)]
        reasons = []
        if any(b is None for b in observation):
            reasons.append("first_eight_complete_minutes_missing")
        else:
            if any(b.open_interest is None for b in observation):
                reasons.append("opening_oi_missing")
            if sum(b.volume for b in observation) <= 0:
                reasons.append("opening_no_volume")
            if any(not b.tradable for b in observation):
                reasons.append("opening_not_tradable")
        if reasons:
            excluded.append(
                {
                    "date": day,
                    "contract": key,
                    "group": entry["group"],
                    "reasons": reasons,
                }
            )
            continue
        r8 = observation[-1].close / observation[0].open - 1
        if r8 == 0:
            excluded.append(
                {
                    "date": day,
                    "contract": key,
                    "group": entry["group"],
                    "reasons": ["r8_zero"],
                }
            )
            continue
        ready.append(
            {k2: v for k2, v in entry.items() if k2 != "meta"}
            | {
                "r8": r8,
                "direction": "LONG" if r8 > 0 else "SHORT",
                "ranking_time": ranking_time.isoformat(),
                "opening": opening.isoformat(),
                "opening_price_definition": "first_minute_open",
                "k": k,
            }
        )
    result = []
    for group in sorted({r["group"] for r in ready}):
        for direction in ("LONG", "SHORT"):
            subset = [
                r for r in ready if r["group"] == group and r["direction"] == direction
            ]
            subset.sort(
                key=lambda r: (-abs(r["r8"]), -r["previous_volume"], r["contract"])
            )
            for rank, row in enumerate(subset, 1):
                result.append(
                    {**row, "rank": rank, "selected": k == "ALL" or rank <= k}
                )
    return result, excluded


def exit_flags(one, past, direction, cfg, entry_check=False):
    flags, diagnostic = [], {"vr_valid": False, "vr": None}
    if one is None:
        return flags, diagnostic
    sign = 1 if direction == "LONG" else -1
    s = cfg["strategy"]
    if finite(one.vr):
        diagnostic = {"vr_valid": True, "vr": float(one.vr)}
        weak = volume_weakness(one, past, direction) if s.get("volume_exit_mode") == "weakness" else None
        if weak is not None:
            diagnostic["volume_weakness"] = weak
        permitted = entry_check or s.get("volume_exit_mode", "threshold") == "threshold" or weak
        if s["enable_volume_exit"] and one.vr >= s["volume_exit_multiple"] and permitted:
            flags.append("volume")
    if s["enable_ma40_exit"] and finite(one.ma40):
        distance = sign * (one.close - one.ma40)
        if distance <= 0:
            flags.append("ma40_cross")
        elif (
            s["ma40_mode"] == "approach" and finite(one.previous_atr) and len(past) >= 3
        ):
            closes = list(past.close.iloc[-3:])
            if (
                distance <= s["ma40_approach_atr"] * one.previous_atr
                and sign * (closes[2] - closes[1]) < 0
                and sign * (closes[1] - closes[0]) < 0
            ):
                flags.append("ma40_approach")
    return flags, diagnostic


def pullback_event(past, direction, mode, tick, cfg):
    if past is None or len(past) < 5:
        return None
    s = cfg["strategy"]
    sign = 1 if direction == "LONG" else -1
    current, previous = past.iloc[-1], past.iloc[-2]
    if sign * (current.close - (previous.high if sign > 0 else previous.low)) <= 0:
        return None
    references = (
        [10]
        if mode == "pullback_ma10"
        else [20]
        if mode == "pullback_ma20"
        else [10, 20]
    )
    # Most recent eligible event first. Window excludes current confirmation candle.
    for index in range(len(past) - 2, max(-1, len(past) - 5), -1):
        row, before = past.iloc[index], past.iloc[index - 1]
        if not finite(row.previous_atr) or row.previous_atr <= 0:
            continue
        if not all(
            finite(before[f"ma{m}"]) and sign * (before.close - before[f"ma{m}"]) > 0
            for m in (10, 20)
        ):
            continue
        epsilon = max(
            s["pullback_epsilon_ticks"] * tick,
            s["pullback_epsilon_atr"] * row.previous_atr,
        )
        price = row.low if sign > 0 else row.high
        touched = [
            m
            for m in (10, 20)
            if finite(row[f"ma{m}"]) and abs(price - row[f"ma{m}"]) <= epsilon
        ]
        if any(m in touched for m in references):
            return {
                "event": past.index[index].isoformat(),
                "references": touched,
                "dual_touch": len(touched) == 2,
                "epsilon": float(epsilon),
            }
    return None


class SignalLogic:
    def __init__(self, dataset, features):
        self.data, self.features = dataset, features
        self._session_rows = {}

    def evaluate(self, bar, candidate, state_allows=True, before_cutoff=True):
        s, cal = self.data.cfg["strategy"], self.data.calendar
        meta = self.data.metadata.get(bar.key, bar.trading_day)
        sign = 1 if candidate["direction"] == "LONG" else -1
        one = self.features.latest(bar.key, 1, bar.end)
        five = self.features.latest(bar.key, 5, bar.end)
        fifteen = self.features.latest(bar.key, 15, bar.end)
        past = self.features.past(bar.key, bar.end, 11 if s.get("trend_quality") else 10)
        opening, _ = cal.bounds(bar.trading_day, meta)
        rows = self.data.by_day[(bar.trading_day, bar.key)]
        identity = bar.trading_day, bar.key
        if identity not in self._session_rows:
            ordered = [(t, b) for t, b in sorted(rows.items()) if t >= opening]
            self._session_rows[identity] = (
                [t for t, _ in ordered],
                [b for _, b in ordered],
                cal.minutes(bar.trading_day, meta),
            )
        times, ordered_bars, expected = self._session_rows[identity]
        session = ordered_bars[: bisect_right(times, bar.datetime)]
        first = rows.get(opening)
        baseline = (
            first.session_open_oi
            if first and first.session_open_oi is not None
            else first.open_interest
            if first
            else None
        )
        oi_delta = (
            bar.open_interest - baseline
            if bar.open_interest is not None and baseline is not None
            else None
        )
        volume = sum(b.volume for b in session)
        elapsed = bisect_right(expected, bar.datetime)
        continuous = len(session) == elapsed
        vwap = None
        if continuous and volume > 0:
            if s["approximate_vwap"]:
                vwap = (
                    sum((b.high + b.low + b.close) / 3 * b.volume for b in session)
                    / volume
                )
            elif meta.get("turnover_factor") and all(
                b.turnover is not None and (b.turnover > 0 or b.volume == 0)
                for b in session
            ):
                vwap = (
                    sum(b.turnover for b in session) / volume / meta["turnover_factor"]
                )
        required = one is not None and all(
            finite(one[n])
            for n in ("ma10", "ma20", "ma40", "previous_atr", "shock", "efficiency")
        )
        higher_ready = all(
            row is not None and all(finite(row[n]) for n in ("ma10", "ma20"))
            for row in (five, fifteen)
        )
        slope_ready = fifteen is not None and finite(fifteen.ma20_slope3)
        current_15 = (
            fifteen is not None
            and str(fifteen.day) == bar.trading_day
            and pd.Timestamp(fifteen.name) >= pd.Timestamp(opening + 15 * MINUTE)
        )
        trend15 = (
            higher_ready
            and slope_ready
            and sign * (fifteen.ma10 - fifteen.ma20) > 0
            and sign * fifteen.ma20_slope3 > 0
            and sign * (fifteen.close - fifteen.ma20) > 0
        )
        trend5 = higher_ready and all(
            sign * (five.close - five[f"ma{m}"]) > 0 for m in (10, 20)
        )
        trend1 = required and all(
            sign * (one.close - one[f"ma{m}"]) > 0 for m in (10, 20)
        )
        efficiency = (
            sign * one.efficiency
            if one is not None and finite(one.efficiency)
            else None
        )
        extension = (
            sign * (one.close - one.ma20) / one.previous_atr
            if required and one.previous_atr > 0
            else None
        )
        flags, diagnostic = exit_flags(one, past, candidate["direction"], self.data.cfg, entry_check=True)
        filters = {
            "candidate": bool(candidate["selected"]),
            "warmup_1m": bool(required),
            "warmup_higher": bool(higher_ready and slope_ready)
            if s["enable_multicycle_filter"]
            else True,
            "current_session_15m": bool(current_15)
            if s["require_current_session_15m"]
            else True,
            "trend_15m": bool(trend15) if s["enable_multicycle_filter"] else True,
            "trend_5m": bool(trend5) if s["enable_multicycle_filter"] else True,
            "trend_1m": bool(trend1),
            "vwap": bool(vwap is not None and sign * (bar.close - vwap) >= 0),
            "oi": bool(oi_delta is not None and oi_delta > 0)
            if s["enable_oi_filter"]
            else True,
            "efficiency": bool(
                efficiency is not None and efficiency >= s["efficiency_min"]
            )
            if s["enable_smooth_filter"]
            else True,
            "shock": bool(required and one.shock < s["shock_max"])
            if s["enable_smooth_filter"]
            else True,
            "extension": bool(extension is not None and extension <= s["extension_max"])
            if s["enable_smooth_filter"]
            else True,
            "no_exit_condition": not flags,
            "state": state_allows,
            "entry_time": before_cutoff,
            "session_data_continuous": continuous,
        }
        quality_snapshot = None
        if rule := s.get("trend_quality"):
            extra_filters, quality_snapshot = trend_quality(past, candidate["direction"], meta.get("tick_size"), float(one.previous_atr) if one is not None else None, rule)
            filters.update(extra_filters)
        slope_snapshot = None
        if band := s.get("slope_band"):
            slope_snapshot = {}
            for label, minutes in (("1m", 1), ("5m", 5)):
                rule = band["timeframes"][label]
                observations = self.features.past_period(bar.key, minutes, bar.end, rule["lookback_bars"] + 1)
                checks, details = slope_band(observations, candidate["direction"], meta.get("tick_size"), rule, band["min_move_ticks"])
                filters.update({f"slope_{label}_{name}": passed for name, passed in checks.items()})
                slope_snapshot[label] = details
        event = (
            pullback_event(
                past,
                candidate["direction"],
                s["entry_mode"],
                meta.get("tick_size") or 0,
                self.data.cfg,
            )
            if s["entry_mode"] != "direct"
            else None
        )
        snapshot = {
            "vwap": vwap,
            "vwap_mode": "approximate_typical"
            if s["approximate_vwap"]
            else "exact_turnover",
            "oi_delta": oi_delta,
            "oi_baseline": "opening_snapshot"
            if first and first.session_open_oi is not None
            else "first_minute_end_proxy",
            "efficiency": efficiency,
            "extension": extension,
            "shock": float(one.shock) if required else None,
            "atr_previous": float(one.previous_atr) if required else None,
            **diagnostic,
        }
        if quality_snapshot is not None:
            snapshot["trend_quality"] = quality_snapshot
        if slope_snapshot is not None:
            snapshot["slope_band"] = slope_snapshot
        if one is not None:
            snapshot.update(
                {
                    f"ma{m}": float(one[f"ma{m}"]) if finite(one[f"ma{m}"]) else None
                    for m in (10, 20, 40)
                }
            )
        return {
            "filters": filters,
            "rejections": [k for k, v in filters.items() if not v],
            "all_pass": all(filters.values()),
            "pullback": event,
            "snapshot": snapshot,
            "exit_flags": flags,
        }
