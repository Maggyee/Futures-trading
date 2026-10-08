import copy
import gzip
import hashlib
import json
from dataclasses import replace

import pandas as pd
import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import load_data
from research.execution import PortfolioBacktest, fee
from research.fixtures import create_fixture
from research.refinements import (
    admissible_entry_price,
    entry_price_guard,
    scaled_protection,
    trend_quality,
    volume_weakness,
)
from research.reporting import write_csv
from research.signals import Features, SignalLogic, exit_flags, rank_candidates
from research.storage import SpaceBudget, write_gzip_json


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY")))


@pytest.mark.parametrize("direction,limit,raw_limit", [("LONG", 101.5, 101.25), ("SHORT", 98.5, 98.75)])
def test_price_boundary_rounding_and_adverse_slippage(direction, limit, raw_limit):
    signal = {"time": "2026-01-08T09:16:00+08:00", "direction": direction, "snapshot": {"ma20": 100, "atr_previous": 1.03}}
    guard = entry_price_guard(signal, {"tick_size": 0.25}, {"extension_max": 1.5, "slippage_ticks": 1})
    assert guard["modeled_price_limit"] == limit
    assert guard["raw_open_price_limit"] == raw_limit
    sign = 1 if direction == "LONG" else -1
    assert admissible_entry_price(guard, limit)
    assert not admissible_entry_price(guard, limit + sign * 0.25)
    signal["snapshot"]["ma20"] = 999
    assert guard["ma20"] == 100


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("miss", [False, True])
def test_opening_price_guard_is_causal_and_releases_reservations(market, direction, miss):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["recheck_entry_price"] = True
    engine = PortfolioBacktest(market, cfg)
    key, day = "aa2603.SHFE", "2026-01-08"
    meta = engine.data.metadata.get(key, day)
    base = engine.data.by_day[(day, key)][at(day, "09:16")]
    sign = 1 if direction == "LONG" else -1
    signal = {"contract": key, "time": base.datetime.isoformat(), "direction": direction, "snapshot": {"ma20": base.open, "atr_previous": 1}, "pullback": None, "rank": 1, "filled": False}
    engine.admit_opportunity(signal, meta, base.open, day, base.datetime)
    state = engine.state(key)
    guard = state.pending["price_guard"]
    quote = guard["raw_open_price_limit"] + (sign * meta["tick_size"] if miss else 0)
    bar = replace(base, open=quote, high=max(quote, guard["raw_open_price_limit"]) + 100, low=min(quote, guard["raw_open_price_limit"]) - 100, close=quote)
    engine.fill_open(key, bar, base.datetime)
    assert signal["fill_price_check"]["accepted"] is (not miss)
    assert signal["filled"] is (not miss)
    if miss:
        assert state.name == "FLAT" and state.pending is None and state.position is None
        assert state.previous_pass  # Needs a new false-to-true signal, not a repeated request.
        assert engine.allocator.usage(engine.states)[:3] == (0, 0, 0)
        assert engine.events[-1]["reason"] == "fill_price_recheck"
    else:
        assert state.position.price == pytest.approx(guard["modeled_price_limit"])


def test_original_configuration_keeps_price_guard_disabled(market):
    engine = PortfolioBacktest(market)
    key, day = "aa2603.SHFE", "2026-01-08"
    bar = engine.data.by_day[(day, key)][at(day, "09:16")]
    signal = {"contract": key, "time": bar.datetime.isoformat(), "direction": "LONG", "snapshot": {}, "pullback": None, "rank": 1, "filled": False}
    engine.admit_opportunity(signal, engine.data.metadata.get(key, day), bar.open, day, bar.datetime)
    assert "price_guard" not in engine.state(key).pending
    engine.fill_open(key, bar, bar.datetime)
    assert signal["filled"] and "fill_price_check" not in signal


def test_price_guard_rejects_invalid_input(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["recheck_entry_price"] = "yes"
    with pytest.raises(ResearchError, match="布尔"):
        validate_config(cfg)
    with pytest.raises(ResearchError, match="正ATR"):
        entry_price_guard({"direction": "LONG", "snapshot": {"ma20": 100, "atr_previous": 0}}, {"tick_size": 1}, {"extension_max": 1.5, "slippage_ticks": 1})


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_efficiency_one_is_not_enough_for_sparse_one_tick_movement(direction):
    sign = 1 if direction == "LONG" else -1
    past = pd.DataFrame({"close": [100] * 10 + [100 + sign]}, index=pd.date_range("2026-01-08 09:00", periods=11, freq="min"))
    rule = {"min_price_changes": 3, "min_displacement_atr": 1, "min_displacement_ticks": 2}
    flags, diagnostic = trend_quality(past, direction, 1, 1, rule)
    assert diagnostic["nonzero_price_changes"] == 1 and diagnostic["signed_move_ticks"] == 1
    assert not flags["trend_activity"] and not flags["trend_displacement"]
    past["close"] = [100 + sign * i for i in range(11)]
    flags, diagnostic = trend_quality(past, direction, 1, 2, rule)
    assert all(flags.values()) and diagnostic["nonzero_price_changes"] == 10


def test_protection_uses_known_volatility_and_cost_without_shrinking_original_stop():
    signal = {"time": "2026-01-08T09:16:00+08:00", "snapshot": {"atr_previous": 12.74}}
    meta = {"product": "AP", "tick_size": 1, "value_per_price": 10}
    strategy = {"protection_scale": {"atr_multiple": 1, "roundtrip_cost_multiple": 2}, "fixed_ticks": {"AP": {"stop_loss_ticks": 6, "take_profit_ticks": 11}}, "slippage_ticks": 1}
    plan = scaled_protection(signal, meta, strategy, 4)
    assert plan["stop_loss_ticks"] == 13 and plan["take_profit_ticks"] == 24
    signal["snapshot"]["atr_previous"] = 1
    plan = scaled_protection(signal, meta, strategy, 4)
    assert plan["stop_loss_ticks"] == 6 and plan["take_profit_ticks"] == 11
    meta["product"] = "FG"
    strategy["fixed_ticks"]["FG"] = {"stop_loss_ticks": 2, "take_profit_ticks": 4}
    plan = scaled_protection(signal, meta, strategy, 4)
    assert plan["stop_loss_ticks"] == 5 and plan["take_profit_ticks"] == 10


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_volume_weakness_requires_both_opposing_candle_and_weaker_close(direction):
    sign = 1 if direction == "LONG" else -1
    past = pd.DataFrame({"close": [100, 100 + sign]}, index=pd.date_range("2026-01-08 09:00", periods=2, freq="min"))
    one = pd.Series({"open": 100, "close": 100 + sign})
    assert not volume_weakness(one, past, direction)
    one.close = 100 - sign
    assert volume_weakness(one, past, direction)
    one.open = 100 - sign * 2  # Weak relative to previous close, favorable candle.
    assert not volume_weakness(one, past, direction)


@pytest.mark.parametrize("kind", ["json", "csv"])
def test_buffered_compression_checks_space_on_final_flush(tmp_path, kind):
    values = [{"value": hashlib.sha256(str(i).encode()).hexdigest()} for i in range(6000)]
    budget = SpaceBudget({"roots": [str(tmp_path)], "max_bytes": 4096, "min_free_bytes": 0})
    path = tmp_path / ("records." + kind + ".gz")
    with pytest.raises(ResearchError, match="空间预算"):
        (write_gzip_json if kind == "json" else write_csv)(path, values, budget)
    assert path.stat().st_size <= 4096
    budget = SpaceBudget({"roots": [str(tmp_path)], "max_bytes": 1024 * 1024, "min_free_bytes": 0})
    (write_gzip_json if kind == "json" else write_csv)(path, values, budget)
    with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
        if kind == "json":
            assert json.load(stream) == values
        else:
            assert len(stream.readlines()) == len(values) + 1


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_scaled_protection_reduces_quantity_and_stays_fixed_while_holding(market, direction):
    cfg = copy.deepcopy(market.cfg)
    # Make the risk budget bind before the fixture's three-lot cap, so this
    # comparison actually exercises resizing under the same risk budget.
    cfg["risk"]["trade_risk_fraction"] = 0.0005
    cfg["strategy"]["protection_scale"] = {"atr_multiple": 1, "roundtrip_cost_multiple": 2}
    engine = PortfolioBacktest(market, cfg)
    key, day = "aa2603.SHFE", "2026-01-08"
    meta = engine.data.metadata.get(key, day)
    bar = engine.data.by_day[(day, key)][at(day, "09:16")]
    signal = {"contract": key, "time": bar.datetime.isoformat(), "direction": direction, "snapshot": {"atr_previous": 20}, "pullback": None, "rank": 1, "r8": 0.01, "filled": False}
    original_cfg = copy.deepcopy(cfg)
    original_cfg["strategy"].pop("protection_scale")
    original = PortfolioBacktest(market, original_cfg)
    original_quantity = original.allocator.allocate(meta, bar.open, day, {}, cfg["risk"]["initial_capital"])[0]
    engine.admit_opportunity(signal, meta, bar.open, day, bar.datetime)
    reserved = engine.state(key).pending["quantity"]
    assert reserved < original_quantity
    engine.fill_open(key, bar, bar.datetime)
    position = engine.state(key).position
    assert position.quantity <= reserved
    assert position.risk <= signal["entry_allocation"]["single_trade_budget"]
    expected = position.quantity * (
        abs(position.price - position.stop) * meta["value_per_price"]
        + cfg["risk"]["cost_buffer_multiple"] * (
            fee(meta, day, "open", position.price, 1) + fee(meta, day, "close_today", position.price, 1)
            + 2 * cfg["strategy"]["slippage_ticks"] * meta["tick_size"] * meta["value_per_price"]
        )
    )
    assert position.risk == pytest.approx(expected)
    stop, target = position.stop, position.target
    signal["snapshot"]["atr_previous"] = 9999
    engine.fill_open(key, replace(bar, open=bar.open + 10), bar.end)
    assert position.stop == stop and position.target == target


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_weak_volume_exit_preserves_original_entry_volume_veto(market, direction):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["volume_exit_mode"] = "weakness"
    sign = 1 if direction == "LONG" else -1
    past = pd.DataFrame({"close": [100, 100 + sign]})
    one = pd.Series({"open": 100, "close": 100 + sign, "ma40": 100 - sign * 5, "vr": 4, "previous_atr": 1})
    assert "volume" not in exit_flags(one, past, direction, cfg)[0]
    assert "volume" in exit_flags(one, past, direction, cfg, entry_check=True)[0]
    one.close = 100 - sign
    assert "volume" in exit_flags(one, past, direction, cfg)[0]


def test_trend_quality_uses_only_completed_observations(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["trend_quality"] = {"min_price_changes": 3, "min_displacement_atr": 1, "min_displacement_ticks": 2}
    engine = PortfolioBacktest(market, cfg)
    day, key = "2026-01-08", "aa2603.SHFE"
    pool, _ = engine.data.pool(day)
    candidate = next(c for c in rank_candidates(engine.data, day, pool, at(day, "09:08"), 1)[0] if c["contract"] == key)
    bar = engine.data.by_day[(day, key)][at(day, "09:20")]
    before = engine.logic.evaluate(bar, candidate)
    assert before["snapshot"]["trend_quality"]["window_end"] == bar.end.isoformat()
    from research.data import Dataset

    changed = Dataset([replace(b, close=b.close + 1000, high=b.high + 1000) if b.key == key and b.datetime > bar.datetime else b for b in engine.data.bars], cfg)
    after = SignalLogic(changed, Features(changed)).evaluate(bar, candidate)
    assert before == after


@pytest.mark.parametrize("option,value", [("trend_quality", {}), ("trend_quality", {"min_price_changes": 0, "min_displacement_atr": 1, "min_displacement_ticks": 2}), ("protection_scale", {"atr_multiple": float("nan"), "roundtrip_cost_multiple": 2}), ("volume_exit_mode", "unknown")])
def test_refinement_config_rejects_invalid_rules(market, option, value):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"][option] = value
    with pytest.raises(ResearchError):
        validate_config(cfg)
