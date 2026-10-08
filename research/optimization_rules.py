"""Causal cost checks, cost-aware break-even prices and afternoon candidates."""

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .calendar import MINUTE
from .config import ResearchError


def cost_check(signal, meta, strategy, roundtrip_fees, price):
    atr = signal["snapshot"].get("atr_previous")
    atr = atr if atr is not None and math.isfinite(atr) and atr > 0 else None
    distance = roundtrip_fees / meta["value_per_price"] + 2 * strategy["slippage_ticks"] * meta["tick_size"]
    ratio = distance / atr if atr is not None and math.isfinite(atr) and atr > 0 else None
    limit = strategy["entry_cost_filter"]["max_cost_atr"]
    return {"price": price, "signal_atr": atr, "roundtrip_fees_per_lot": roundtrip_fees,
            "roundtrip_price_distance": distance, "cost_atr": ratio, "max_cost_atr": limit,
            "accepted": ratio is not None and ratio <= limit + 1e-12,
            "fee_basis": "entry_time_known_exchange_schedule", "slippage_both_sides": strategy["slippage_ticks"]}


def initial_breakeven(position, rule, slippage_ticks, activation_distance=None):
    sign, meta = position.sign, position.meta
    schedules = [r for r in meta["fees"] if r["effective_from"] <= position.entry_day
                 and (not r.get("effective_to") or position.entry_day <= r["effective_to"])]
    if not schedules:
        raise ResearchError("保本价格缺少开仓时已知的费用规则")
    schedule = max(schedules, key=lambda r: r["effective_from"])["close_today"]
    tick, value = Decimal(str(meta["tick_size"])), Decimal(str(meta["value_per_price"]))
    open_fee = Decimal(str(position.entry_fee)) / position.quantity / value
    fixed = Decimal(str(schedule["value"])) / value if schedule["mode"] == "fixed" else Decimal(0)
    rate = Decimal(str(schedule["value"])) if schedule["mode"] == "rate" else Decimal(0)
    modeled = (Decimal(str(position.price)) + sign * (open_fee + fixed)) / (1 - sign * rate)
    raw = modeled + sign * slippage_ticks * tick
    rounded = (raw / tick).to_integral_value(rounding=ROUND_CEILING if sign > 0 else ROUND_FLOOR) * tick
    risk_distance = abs(position.price - position.stop)
    activation = rule["activation_r"] * risk_distance if activation_distance is None else activation_distance
    return {"breakeven_active": False, "breakeven_armed_at": None,
            "breakeven_activation_price": position.price + sign * activation,
            "breakeven_price": float(rounded), "breakeven_fee_basis": "entry_time_known_close_today",
            "breakeven_slippage_ticks": slippage_ticks}


def protection_reason(position):
    state = getattr(position, "trailing", None)
    if state is None or position.sign * (state["stop_price"] - position.stop) <= 0:
        return "fixed_stop"
    if state.get("breakeven_active") and abs(state["stop_price"] - state["breakeven_price"]) <= position.meta["tick_size"] * 1e-8:
        return "breakeven_stop"
    return "trailing_stop"


def afternoon_candidates(dataset, day, pool, opening, k, minutes=8, cutoff=None):
    if cutoff != opening + minutes * MINUTE:
        raise ResearchError("午后排名须等待已声明的完整观察窗口")
    ready, excluded = [], []
    for entry in pool:
        meta, key = entry["meta"], entry["contract"]
        afternoons = [a for a, _ in dataset.calendar.periods(day, meta) if a.hour >= 12]
        if not afternoons or afternoons[0] != opening:
            continue
        observation = [dataset.by_day.get((day, key), {}).get(opening + i * MINUTE) for i in range(minutes)]
        reasons = []
        if any(b is None for b in observation):
            reasons.append("afternoon_complete_minutes_missing")
        elif any(b.open_interest is None or not b.tradable for b in observation) or sum(b.volume for b in observation) <= 0:
            reasons.append("afternoon_observation_unavailable")
        if reasons:
            excluded.append({"date": day, "contract": key, "group": entry["group"], "reasons": reasons})
            continue
        change = observation[-1].close / observation[0].open - 1
        if not change:
            continue
        ready.append({name: value for name, value in entry.items() if name != "meta"} |
                     {"r8": change, "direction": "LONG" if change > 0 else "SHORT", "k": k,
                      "ranking_time": (opening + minutes * MINUTE).isoformat(), "opening": opening.isoformat(),
                      "opening_price_definition": "afternoon_first_minute_open", "ranking_phase": "afternoon"})
    result = []
    for group in sorted({r["group"] for r in ready}):
        for direction in ("LONG", "SHORT"):
            subset = sorted((r for r in ready if r["group"] == group and r["direction"] == direction),
                            key=lambda r: (-abs(r["r8"]), -r["previous_volume"], r["contract"]))
            result.extend(row | {"rank": rank, "selected": rank <= k} for rank, row in enumerate(subset, 1))
    return result, excluded
