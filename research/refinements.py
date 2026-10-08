"""Explicit research variants; original configurations keep their old behavior."""

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import ResearchError


def entry_price_guard(signal, meta, strategy):
    """Freeze an admissible price from information known at the signal close.

    The raw quote cap also reserves the existing adverse slippage allowance.
    Matching only checks the next available opening quote, never that minute's
    later extrema. A rejected opening does not become a retrospective limit fill.
    """
    snapshot = signal["snapshot"]
    ma20, atr = snapshot.get("ma20"), snapshot.get("atr_previous")
    if not all(v is not None and math.isfinite(v) for v in (ma20, atr)) or atr <= 0:
        raise ResearchError("入场价格边界缺少信号时已知的MA20/正ATR")
    sign = 1 if signal["direction"] == "LONG" else -1
    tick = Decimal(str(meta["tick_size"]))
    boundary = Decimal(str(ma20)) + sign * Decimal(str(strategy["extension_max"])) * Decimal(str(atr))
    units = (boundary / tick).to_integral_value(rounding=ROUND_FLOOR if sign > 0 else ROUND_CEILING)
    limit = units * tick
    return {
        "direction": signal["direction"], "signal_time": signal["time"],
        "ma20": ma20, "atr_previous": atr, "extension_max": strategy["extension_max"],
        "continuous_boundary": float(boundary), "modeled_price_limit": float(limit),
        "raw_open_price_limit": float(limit - sign * Decimal(str(strategy["slippage_ticks"])) * tick),
        "tick_size": float(tick), "matching": "next_available_open_or_cancel",
    }


def admissible_entry_price(guard, modeled_price):
    sign = 1 if guard["direction"] == "LONG" else -1
    return sign * (modeled_price - guard["modeled_price_limit"]) <= guard["tick_size"] * 1e-8


def trend_quality(past, direction, tick, atr, rule):
    diagnostics = {"observations": len(past) if past is not None else 0}
    if past is None or len(past) < 11 or atr is None or not math.isfinite(atr) or atr <= 0 or tick is None or tick <= 0:
        return {"trend_activity": False, "trend_displacement": False}, diagnostics
    closes = list(past.close.iloc[-11:])
    if not all(math.isfinite(v) for v in closes):
        return {"trend_activity": False, "trend_displacement": False}, diagnostics
    sign = 1 if direction == "LONG" else -1
    changes = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    move = sign * (closes[-1] - closes[0])
    count = sum(abs(change) > tick * 1e-8 for change in changes)
    threshold = max(rule["min_displacement_atr"] * atr, rule["min_displacement_ticks"] * tick)
    diagnostics.update({
        "window_start": past.index[-11].isoformat(), "window_end": past.index[-1].isoformat(),
        "signed_move_ticks": move / tick, "signed_move_atr": move / atr,
        "nonzero_price_changes": count, "required_displacement": threshold,
    })
    if "ma20" in past and len(past) >= 6:
        slope = sign * (past.ma20.iloc[-1] - past.ma20.iloc[-6]) / tick
        diagnostics["signed_ma20_slope5_ticks"] = slope if math.isfinite(slope) else None
    if "ma40" in past:
        distance = sign * (closes[-1] - past.ma40.iloc[-1]) / tick
        diagnostics["signed_ma40_distance_ticks"] = distance if math.isfinite(distance) else None
    return {"trend_activity": count >= rule["min_price_changes"], "trend_displacement": move >= threshold - tick * 1e-8}, diagnostics


def slope_band(past, direction, tick, rule, min_move_ticks):
    """Bound the directional MA20 change using this period's causal ATR.

    The tick floor prevents near-zero ATR from amplifying negligible movement.
    Lookbacks count completed observations, including across session breaks.
    """
    count = rule["lookback_bars"]
    diagnostics = {
        "observations": len(past) if past is not None else 0,
        "lookback_bars": count,
        "min_atr_per_bar": rule["min_atr_per_bar"],
        "max_atr_per_bar": rule["max_atr_per_bar"],
        "min_move_ticks": min_move_ticks,
    }
    rejected = {"ready": False, "minimum": False, "maximum": False}
    if past is None or len(past) <= count or direction not in {"LONG", "SHORT"}:
        return rejected, diagnostics
    start, end = past.iloc[-count - 1], past.iloc[-1]
    values = (start.get("ma20"), end.get("ma20"), end.get("previous_atr"), tick)
    if any(v is None or not math.isfinite(v) for v in values) or values[2] <= 0 or tick <= 0:
        return rejected, diagnostics
    sign = 1 if direction == "LONG" else -1
    move = sign * (values[1] - values[0])
    normalized, ticks = move / (count * values[2]), move / tick
    if not math.isfinite(normalized) or not math.isfinite(ticks):
        return rejected, diagnostics
    diagnostics.update({
        "window_start": past.index[-count - 1].isoformat(),
        "window_end": past.index[-1].isoformat(),
        "ma20_start": float(values[0]), "ma20_end": float(values[1]),
        "atr_previous": float(values[2]),
        "signed_atr_per_bar": float(normalized), "signed_move_ticks": float(ticks),
    })
    return {
        "ready": True,
        "minimum": bool(normalized >= rule["min_atr_per_bar"] - 1e-12 and ticks >= min_move_ticks - 1e-8),
        "maximum": bool(normalized <= rule["max_atr_per_bar"] + 1e-12),
    }, diagnostics


def scaled_protection(signal, meta, strategy, roundtrip_fees, minimum_stop_ticks=0):
    scale = strategy["protection_scale"]
    original = strategy["fixed_ticks"][meta["product"]]
    atr = signal["snapshot"]["atr_previous"]
    if atr is None or not math.isfinite(atr) or atr <= 0:
        raise ResearchError("逐笔固定保护缺少开仓前已知的正ATR")
    tick = meta["tick_size"]
    costs = roundtrip_fees / (tick * meta["value_per_price"]) + 2 * strategy["slippage_ticks"]
    atr_floor = math.ceil(scale["atr_multiple"] * atr / tick - 1e-9)
    cost_floor = math.ceil(scale["roundtrip_cost_multiple"] * costs - 1e-9)
    stop = max(original["stop_loss_ticks"], atr_floor, cost_floor, minimum_stop_ticks)
    target = math.ceil(stop * original["take_profit_ticks"] / original["stop_loss_ticks"] - 1e-9)
    return {
        "stop_loss_ticks": stop, "take_profit_ticks": target,
        "original_training_stop_ticks": original["stop_loss_ticks"],
        "original_training_target_ticks": original["take_profit_ticks"],
        "signal_atr": atr, "atr_floor_ticks": atr_floor, "cost_floor_ticks": cost_floor,
        "roundtrip_fee_cny_per_lot": roundtrip_fees, "roundtrip_fee_and_slippage_ticks": costs,
        "signal_time": signal["time"], "frozen_after_fill": True,
    }


def volume_weakness(one, past, direction):
    if past is None or len(past) < 2:
        return False
    sign = 1 if direction == "LONG" else -1
    previous_close = past.close.iloc[-2]
    return bool(sign * (one.close - one.open) < 0 and sign * (one.close - previous_close) < 0)
