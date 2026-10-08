"""Fractional-tick audit accepts numerical roundoff, never price or clock changes."""

import importlib.util
from pathlib import Path

import pytest

from research.config import ResearchError

spec = importlib.util.spec_from_file_location(
    "fractional_tick_audit",
    Path(__file__).resolve().parents[1]
    / "research_inputs/2026-09/audit_trailing_exit.py",
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def state():
    return {
        "breakeven_activation_price": 2851.4,
        "stop_price": 2853.2,
        "active": False,
        "breakeven_active": True,
        "known_at": "2026-07-14T11:06:00+08:00",
        "atr_multiple": 2.0,
    }


def test_fractional_tick_price_roundoff_is_accepted():
    expected = state()
    actual = expected | {"breakeven_activation_price": 2851.3999999999996}
    audit.compare_final_state(actual, expected, 0.2)


@pytest.mark.parametrize(
    "change",
    [
        {"stop_price": 2853.4},
        {"breakeven_activation_price": 2851.400001},
        {"breakeven_activation_price": float("nan")},
        {"known_at": "2026-07-14T11:05:00+08:00"},
        {"breakeven_active": False},
        {"breakeven_active": 1},
        {"extra_state": 0},
    ],
)
def test_prices_states_and_timestamps_cannot_silently_change(change):
    with pytest.raises(ResearchError):
        audit.compare_final_state(state() | change, state(), 0.2)
