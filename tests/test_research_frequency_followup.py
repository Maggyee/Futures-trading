"""Causal behavior tests for the optional stop/re-entry gate and replay inputs."""

import copy
import json
from dataclasses import replace

import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import Dataset, load_data
from research.execution import PortfolioBacktest, Position
from research.fixtures import create_fixture
from research.frequency_followup import recompute_saved

DAY, NEXT_DAY, KEY = "2026-01-08", "2026-01-09", "aa2603.SHFE"


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(
        create_fixture(tmp_path_factory.mktemp("SYNTHETIC_FREQUENCY_TEST_ONLY"))
    )


def configured(market, enabled):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"].update(
        block_same_day_reentry_after_stop=enabled,
        enable_volume_exit=False,
        enable_ma40_exit=False,
    )
    cfg["strategy"]["cooldown_minutes"] = 0
    bars = [replace(b, open=100, high=100.1, low=99.9, close=100) for b in market.bars]
    engine = PortfolioBacktest(Dataset(bars, cfg))
    return engine


def seed_position(engine, day=DAY, sign=1):
    meta = engine.data.metadata.get(KEY, day)
    signal = {
        "time": at(day, "09:15").isoformat(),
        "pullback": None,
        "rank": 1,
        "r8": 0.01,
        "snapshot": {},
    }
    p = Position(
        KEY,
        meta,
        sign,
        1,
        100,
        100,
        at(day, "09:15"),
        day,
        98 if sign > 0 else 102,
        104 if sign > 0 else 96,
        1,
        100,
        100,
        signal,
    )
    engine.state(KEY).position = p
    engine.state(KEY).name = "LONG" if sign > 0 else "SHORT"
    return p


@pytest.mark.parametrize("sign", [1, -1])
def test_only_executed_losing_initial_stop_blocks_that_day_contract_and_side(
    market, sign
):
    engine = configured(market, True)
    seed_position(engine, sign=sign)
    bar = engine.data.by_day[(DAY, KEY)][at(DAY, "09:16")]
    assert engine.stopped_sides == set()
    assert engine.close(KEY, bar, 100 - sign * 2, bar.end, ["fixed_stop"])
    side = "LONG" if sign > 0 else "SHORT"
    assert engine.stopped_sides == {(DAY, KEY, side)}
    assert (NEXT_DAY, KEY, side) not in engine.stopped_sides
    assert (DAY, KEY, "SHORT" if sign > 0 else "LONG") not in engine.stopped_sides
    assert any(
        e["action"] == "same_day_reentry_blocked" and e["time"] == bar.end.isoformat()
        for e in engine.events
    )


@pytest.mark.parametrize(
    "enabled,reason,raw",
    [
        (False, "fixed_stop", 98),
        (True, "volume", 98),
        (True, "trailing_stop", 103),
        (True, "fixed_stop", 102),
    ],
)
def test_other_exits_or_disabled_gate_do_not_block_entries(
    market, enabled, reason, raw
):
    engine = configured(market, enabled)
    seed_position(engine)
    bar = engine.data.by_day[(DAY, KEY)][at(DAY, "09:16")]
    assert engine.close(KEY, bar, raw, bar.end, [reason])
    assert not engine.stopped_sides


def test_unfilled_exit_does_not_create_stop_history(market, monkeypatch):
    engine = configured(market, True)
    seed_position(engine)
    bar = engine.data.by_day[(DAY, KEY)][at(DAY, "09:16")]
    monkeypatch.setattr(
        engine.parameters, "resolve", lambda key, time: (None, ["missing_rule"])
    )
    assert not engine.close(KEY, bar, 98, bar.end, ["fixed_stop"])
    assert engine.state(KEY).position is not None and not engine.stopped_sides


def test_gate_suppresses_real_trigger_but_expires_next_trading_day(market):
    engine = configured(market, True)
    engine.stopped_sides.add((DAY, KEY, "LONG"))
    # The synthetic aa series is ranked LONG by its genuine opening return.
    for day in [DAY, NEXT_DAY]:
        opening = engine.data.by_day[(day, KEY)][at(day, "09:00")]
        replacement = replace(opening, open=99, low=98.9)
        engine.data.by_day[(day, KEY)][opening.datetime] = replacement

    def signal(bar, candidate, state_allows=True, before_cutoff=True):
        active = bar.key == KEY and bar.end == at(bar.trading_day, "09:15")
        return {
            "filters": {
                "state": state_allows,
                "manual": active,
                "entry_time": before_cutoff,
            },
            "all_pass": active and state_allows and before_cutoff,
            "rejections": [],
            "pullback": None,
            "snapshot": {},
            "exit_flags": [],
        }

    engine.logic.evaluate = signal
    result = engine.run(DAY, NEXT_DAY)
    first = next(
        s
        for s in result["signals"]
        if s["contract"] == KEY and s["time"] == at(DAY, "09:15").isoformat()
    )
    next_day = next(
        s
        for s in result["signals"]
        if s["contract"] == KEY and s["time"] == at(NEXT_DAY, "09:15").isoformat()
    )
    assert first["direction"] == next_day["direction"] == "LONG"
    assert not first["trigger"] and not first["filters"]["stop_reentry"]
    assert next_day["trigger"] and next_day["filled"]
    assert result["trades"] and all(
        t["entry_time"][:10] == NEXT_DAY for t in result["trades"]
    )


@pytest.mark.parametrize("bad", [1, "true", None])
def test_reentry_config_requires_boolean(market, bad):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["block_same_day_reentry_after_stop"] = bad
    with pytest.raises(ResearchError):
        validate_config(cfg)


def test_saved_entry_recomputes_thresholds_without_reusing_state_cost_or_fills():
    point = {
        "lookback_bars": 3,
        "min_atr_per_bar": 0.04,
        "max_atr_per_bar": 0.2,
        "min_move_ticks": 1,
        "signed_atr_per_bar": 0.03,
        "signed_move_ticks": 2,
    }
    strategy = {
        "efficiency_min": 0.35,
        "enable_oi_filter": False,
        "slope_band": {
            "min_move_ticks": 1,
            "timeframes": {
                p: {"lookback_bars": 3, "min_atr_per_bar": 0.02, "max_atr_per_bar": 0.3}
                for p in ["1m", "5m"]
            },
        },
    }
    snapshot = {
        "efficiency": 0.4,
        "oi_delta": -100,
        "slope_band": {"1m": point, "5m": point},
    }
    row = {
        "filters": json.dumps(
            {
                "state": False,
                "efficiency": False,
                "oi": False,
                "cost": False,
                "stop_reentry": False,
                **{
                    f"slope_{p}_{k}": v
                    for p in ["1m", "5m"]
                    for k, v in [("ready", True), ("minimum", False), ("maximum", True)]
                },
            }
        ),
        "snapshot": json.dumps(snapshot),
        "filled": "True",
        "net_pnl": "999999",
    }
    filters, updated = recompute_saved(row, strategy)
    assert filters["efficiency"] and filters["oi"] and filters["slope_5m_minimum"]
    assert "cost" not in filters and "stop_reentry" not in filters
    assert (
        updated["oi_delta"] == -100
        and updated["slope_band"]["5m"]["signed_atr_per_bar"] == 0.03
    )
    assert updated["slope_band"]["5m"]["min_atr_per_bar"] == 0.02
