import copy
from dataclasses import replace

import pandas as pd
import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import Dataset, load_data
from research.execution import PortfolioBacktest
from research.fixtures import create_fixture
from research.refinements import slope_band
from research.signals import Features, SignalLogic, rank_candidates

RULE = {"lookback_bars": 5, "min_atr_per_bar": 0.1, "max_atr_per_bar": 0.3}
BAND = {"min_move_ticks": 1.0, "timeframes": {"1m": RULE, "5m": {**RULE, "lookback_bars": 3}}}


def observations(move, atr=2, lookback=5):
    return pd.DataFrame(
        {"ma20": [100 + move * i / lookback for i in range(lookback + 1)], "previous_atr": atr},
        index=pd.date_range("2026-01-08 09:00", periods=lookback + 1, freq="min"),
    )


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("move,minimum,maximum", [(0, False, True), (-2, False, True), (0.5, False, True), (1, True, True), (2, True, True), (3, True, True), (4, True, False)])
def test_band_rejects_flat_opposite_and_steep_with_inclusive_boundaries(direction, move, minimum, maximum):
    sign = 1 if direction == "LONG" else -1
    flags, snapshot = slope_band(observations(sign * move), direction, 1, RULE, 1)
    assert flags == {"ready": True, "minimum": minimum, "maximum": maximum}
    assert snapshot["signed_atr_per_bar"] == pytest.approx(move / 10)
    assert snapshot["signed_move_ticks"] == pytest.approx(move)


def test_tiny_atr_does_not_turn_subtick_movement_into_acceptable_trend():
    flags, snapshot = slope_band(observations(0.001, atr=0.001), "LONG", 1, RULE, 1)
    assert 0.1 < snapshot["signed_atr_per_bar"] < 0.3
    assert flags["ready"] and not flags["minimum"]


@pytest.mark.parametrize("invalid", [None, "short", "nan_ma", "nan_atr", "zero_atr", "missing_atr", "zero_tick"])
def test_unknown_slope_rejects_entry(invalid):
    past, tick = observations(2), 1
    if invalid is None:
        past = None
    elif invalid == "short":
        past = past.iloc[1:]
    elif invalid == "nan_ma":
        past.iloc[0, 0] = float("nan")
    elif invalid == "nan_atr":
        past["previous_atr"] = float("nan")
    elif invalid == "zero_atr":
        past["previous_atr"] = 0
    elif invalid == "missing_atr":
        past = past.drop(columns="previous_atr")
    else:
        tick = 0
    assert not any(slope_band(past, "LONG", tick, RULE, 1)[0].values())


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY")))


@pytest.mark.parametrize("mutation", ["floor_bool", "floor_nan", "missing_period", "extra_period", "lookback_bool", "lookback_large", "reversed", "zero", "nan", "extra_field"])
def test_slope_config_rejects_invalid_bands(market, mutation):
    cfg = copy.deepcopy(market.cfg)
    band = cfg["strategy"]["slope_band"] = copy.deepcopy(BAND)
    rule = band["timeframes"]["1m"]
    if mutation == "floor_bool":
        band["min_move_ticks"] = True
    elif mutation == "floor_nan":
        band["min_move_ticks"] = float("nan")
    elif mutation == "missing_period":
        band["timeframes"].pop("5m")
    elif mutation == "extra_period":
        band["timeframes"]["15m"] = rule.copy()
    elif mutation == "lookback_bool":
        rule["lookback_bars"] = True
    elif mutation == "lookback_large":
        rule["lookback_bars"] = 21
    elif mutation == "reversed":
        rule["max_atr_per_bar"] = rule["min_atr_per_bar"]
    elif mutation == "zero":
        rule["min_atr_per_bar"] = 0
    elif mutation == "nan":
        rule["max_atr_per_bar"] = float("nan")
    else:
        rule["unknown"] = 1
    with pytest.raises(ResearchError, match="斜率"):
        validate_config(cfg)


def test_signal_uses_completed_higher_bars_and_is_invariant_to_future_prices(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["slope_band"] = copy.deepcopy(BAND)
    validate_config(cfg)
    engine = PortfolioBacktest(market, cfg)
    day, key = "2026-01-08", "aa2603.SHFE"
    pool, _ = engine.data.pool(day)
    candidate = next(c for c in rank_candidates(engine.data, day, pool, at(day, "09:08"), 1)[0] if c["contract"] == key)
    bar = engine.data.by_day[(day, key)][at(day, "09:22")]
    before = engine.logic.evaluate(bar, candidate)
    slope = before["snapshot"]["slope_band"]
    assert slope["1m"]["window_end"] == bar.end.isoformat()
    assert slope["5m"]["window_end"] == at(day, "09:20").isoformat()
    changed = Dataset([replace(b, close=b.close + 1000, high=b.high + 1000) if b.key == key and b.datetime > bar.datetime else b for b in engine.data.bars], cfg)
    assert before == SignalLogic(changed, Features(changed)).evaluate(bar, candidate)
    # The added filters must control all_pass, not merely appear in diagnostics.
    assert before["all_pass"] == all(before["filters"].values())
    assert {k for k in before["filters"] if k.startswith("slope_")} == {f"slope_{p}_{f}" for p in ("1m", "5m") for f in ("ready", "minimum", "maximum")}


def test_disabled_slope_keeps_legacy_signals(market):
    engine = PortfolioBacktest(market)
    day, key = "2026-01-08", "aa2603.SHFE"
    pool, _ = engine.data.pool(day)
    candidate = next(c for c in rank_candidates(engine.data, day, pool, at(day, "09:08"), 1)[0] if c["contract"] == key)
    bar = engine.data.by_day[(day, key)][at(day, "09:22")]
    old = engine.logic.evaluate(bar, candidate)
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["slope_band"] = None
    assert old == PortfolioBacktest(market, cfg).logic.evaluate(bar, candidate)
    assert "slope_band" not in old["snapshot"]
    assert not any(k.startswith("slope_") for k in old["filters"])
