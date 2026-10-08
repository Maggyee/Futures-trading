"""Causal, monotone trailing protection; no matching against a newly seen high."""

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def initial_trailing(position, rule, breakeven_rule=None, slippage_ticks=0):
    protection = position.signal.get("entry_protection", {})
    state = {
        "active": False,
        "activation_price": position.target,
        "initial_hard_stop": position.stop,
        "best_price": position.price,
        "stop_price": position.stop,
        "atr_multiple": protection.get("trailing_atr_multiple", rule["atr_multiple"]),
        "armed_at": None,
        "known_at": position.opened.isoformat(),
    }
    if breakeven_rule:
        from .optimization_rules import initial_breakeven
        state.update(initial_breakeven(position, breakeven_rule, slippage_ticks,
                     protection.get("breakeven_activation_distance")))
    return state


def advance_trailing(position, bar, previous_atr):
    """Called at candle end, after that candle's previously known protection."""
    state, sign = position.trailing, position.sign
    diagnostic = {
        "entry_time": position.opened.isoformat(), "bar_end": bar.end.isoformat(),
        "previous_stop": state["stop_price"], "initial_hard_stop": position.stop,
        "activation_price": state["activation_price"], "close": bar.close,
        "effective_from": "next_available_open", "close_exit_requested": False,
    }
    if bar.volume <= 0:
        return {**diagnostic, "available": False, "reason": "nonpositive_volume"}
    extreme = bar.high if sign > 0 else bar.low
    best = max(state["best_price"], extreme) if sign > 0 else min(state["best_price"], extreme)
    state["best_price"] = best
    if "breakeven_price" in state:
        if not state["breakeven_active"] and sign * (best - state["breakeven_activation_price"]) >= 0:
            state["breakeven_active"] = True
            state["breakeven_armed_at"] = bar.end.isoformat()
        if state["breakeven_active"]:
            state["stop_price"] = max(state["stop_price"], state["breakeven_price"]) if sign > 0 else min(state["stop_price"], state["breakeven_price"])
            state["known_at"] = bar.end.isoformat()
        diagnostic.update({key: state[key] for key in ("breakeven_active", "breakeven_armed_at", "breakeven_activation_price", "breakeven_price")})
        diagnostic["close_exit_requested"] = sign * (bar.close - state["stop_price"]) <= 0 and sign * (state["stop_price"] - position.stop) > 0
    if not state["active"] and sign * (best - state["activation_price"]) >= 0:
        state["active"] = True
        state["armed_at"] = bar.end.isoformat()
    diagnostic.update(best_price=best, active=state["active"], atr_previous=previous_atr)
    if previous_atr is None or not math.isfinite(previous_atr) or previous_atr <= 0:
        diagnostic["atr_previous"] = None
        return {**diagnostic, "available": False, "reason": "invalid_atr", "new_stop": state["stop_price"]}
    if not state["active"]:
        return {**diagnostic, "available": True, "new_stop": state["stop_price"], "tightened": sign * (state["stop_price"] - diagnostic["previous_stop"]) > 0}
    tick = Decimal(str(position.meta["tick_size"]))
    distance_ticks = math.ceil(state["atr_multiple"] * previous_atr / float(tick) - 1e-9)
    distance_ticks = max(1, distance_ticks)
    continuous = Decimal(str(best)) - sign * distance_ticks * tick
    rounding = ROUND_FLOOR if sign > 0 else ROUND_CEILING
    candidate = float((continuous / tick).to_integral_value(rounding=rounding) * tick)
    updated = max(state["stop_price"], position.stop, candidate) if sign > 0 else min(state["stop_price"], position.stop, candidate)
    state["stop_price"], state["known_at"] = updated, bar.end.isoformat()
    diagnostic.update({
        "available": True, "distance_ticks": distance_ticks, "candidate_stop": candidate,
        "new_stop": updated, "tightened": sign * (updated - diagnostic["previous_stop"]) > 0,
        "close_exit_requested": sign * (bar.close - updated) <= 0 and sign * (updated - position.stop) > 0,
    })
    return diagnostic
