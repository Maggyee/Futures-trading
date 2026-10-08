import copy
from dataclasses import replace

import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import Dataset, load_data
from research.execution import PortfolioBacktest, Position, protective_touch
from research.fixtures import create_fixture
from research.trailing import advance_trailing, initial_trailing

DAY, KEY = "2026-01-08", "aa2603.SHFE"
RULE = {"activation": "original_target", "atr_multiple": 2.0}


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY")))


def position(market, sign=1):
    meta = dict(market.metadata.get(KEY, DAY), tick_size=0.25)
    p = Position(KEY, meta, sign, 1, 100, 100, at(DAY, "09:15"), DAY, 100 - sign * 2, 100 + sign * 4, 0, 100, 100, {})
    p.trailing = initial_trailing(p, RULE)
    return p


def candle(market, sign=1, maximum=6, adverse=-1, close=5, opening=0, minute="09:15"):
    values = [100 + sign * maximum, 100 + sign * adverse]
    return replace(market.by_day[(DAY, KEY)][at(DAY, minute)], open=100 + sign * opening, high=max(values), low=min(values), close=100 + sign * close)


@pytest.mark.parametrize("sign", [1, -1])
def test_target_activates_after_close_without_retrospective_same_bar_fill(market, sign):
    p, bar = position(market, sign), candle(market, sign, close=3)
    assert protective_touch(p, bar) is None  # Original hard stop survives; target no longer exits.
    observation = advance_trailing(p, bar, 1)
    assert p.trailing["active"] and p.trailing["stop_price"] == 100 + sign * 4
    assert observation["close_exit_requested"]
    assert observation["effective_from"] == "next_available_open"
    # A later tradable opening is used, including adverse gaps.
    next_bar = candle(market, sign, maximum=4, adverse=1, close=2, opening=2, minute="09:16")
    hit = protective_touch(p, next_bar)
    assert hit["reason"] == "trailing_stop" and hit["at_open"] and hit["gap"]
    assert hit["raw_price"] == next_bar.open
    assert p.stop == 100 - sign * 2 and p.target == 100 + sign * 4


@pytest.mark.parametrize("sign", [1, -1])
def test_trail_only_tightens_even_if_atr_expands_or_price_retraces(market, sign):
    p = position(market, sign)
    advance_trailing(p, candle(market, sign), 1)
    first = p.trailing["stop_price"]
    result = advance_trailing(p, candle(market, sign, maximum=7, close=6, minute="09:16"), 10)
    assert p.trailing["stop_price"] == first and not result["tightened"]
    advance_trailing(p, candle(market, sign, maximum=8, close=7, minute="09:17"), 1)
    assert sign * (p.trailing["stop_price"] - first) == 2
    advance_trailing(p, candle(market, sign, maximum=7, close=6, minute="09:18"), 1)
    assert p.trailing["best_price"] == 100 + sign * 8
    assert p.trailing["stop_price"] == 100 + sign * 6


@pytest.mark.parametrize("sign", [1, -1])
def test_hard_stop_still_precedes_a_target_touch_in_same_candle(market, sign):
    p = position(market, sign)
    hit = protective_touch(p, candle(market, sign, maximum=8, adverse=-3))
    assert hit["reason"] == "fixed_stop" and hit["raw_price"] == p.stop
    assert not p.trailing["active"]


@pytest.mark.parametrize("sign", [1, -1])
def test_tick_rounding_reserves_full_atr_distance(market, sign):
    p = position(market, sign)
    observation = advance_trailing(p, candle(market, sign, maximum=7.37, close=7), 1.03)
    assert observation["distance_ticks"] == 9
    assert observation["new_stop"] == 100 + sign * 5
    assert sign * (p.trailing["best_price"] - observation["new_stop"]) >= 2.06


@pytest.mark.parametrize("invalid", [None, 0, float("nan"), float("inf")])
def test_missing_atr_preserves_existing_protection(market, invalid):
    p = position(market)
    advance_trailing(p, candle(market), 1)
    old = p.trailing["stop_price"]
    result = advance_trailing(p, candle(market, maximum=10), invalid)
    assert not result["available"] and p.trailing["stop_price"] == old


def test_no_volume_cannot_arm_or_move_trailing_line(market):
    p = position(market)
    old = copy.deepcopy(p.trailing)
    result = advance_trailing(p, replace(candle(market, maximum=100), volume=0), 1)
    assert not result["available"] and p.trailing == old


def test_no_activation_before_original_target(market):
    p = position(market)
    result = advance_trailing(p, candle(market, maximum=3, close=2), 0.1)
    assert not p.trailing["active"] and not result["close_exit_requested"]
    assert p.trailing["stop_price"] == p.stop


@pytest.mark.parametrize("rule", [{}, False, {"activation": "profit", "atr_multiple": 2}, {"activation": "original_target", "atr_multiple": True}, {"activation": "original_target", "atr_multiple": 0}, {"activation": "original_target", "atr_multiple": float("nan")}, {"activation": "original_target", "atr_multiple": float("inf")}])
def test_invalid_trailing_rule_rejected(market, rule):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["trailing_exit"] = rule
    with pytest.raises(ResearchError, match="追踪"):
        validate_config(cfg)


def test_close_breach_exits_next_open_even_if_price_recovers_and_fill_is_delayed(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"].update(trailing_exit=RULE, enable_volume_exit=False, enable_ma40_exit=False)
    cfg["strategy"]["fixed_ticks"]["aa"] = {"stop_loss_ticks": 100, "take_profit_ticks": 200}
    bars = []
    for b in market.bars:
        if b.key == KEY and b.trading_day == DAY:
            if b.datetime == at(DAY, "09:15"):
                b = replace(b, open=100, high=103, low=99.5, close=100.5)
            elif b.datetime == at(DAY, "09:16"):
                b = replace(b, open=104, high=9999, low=103, close=104, volume=0)
            elif b.datetime == at(DAY, "09:17"):
                b = replace(b, open=105, high=9999, low=104, close=105)
        bars.append(b)
    engine = PortfolioBacktest(Dataset(bars, cfg))
    engine.features.frames[(KEY, 1)]["previous_atr"] = 0.5

    def manual_entry(bar, candidate, state_allows=True, before_cutoff=True):
        passing = bar.key == KEY and bar.end == at(DAY, "09:15") and state_allows
        return {"filters": {"state": state_allows, "manual_test_entry": passing}, "rejections": [] if passing else ["manual_test_entry"], "all_pass": passing, "pullback": None, "snapshot": {}, "exit_flags": []}

    engine.logic.evaluate = manual_entry
    result = engine.run(DAY, DAY)
    assert len(result["trades"]) == 1
    t = result["trades"][0]
    assert t["exit_time"] == at(DAY, "09:17").isoformat()
    assert t["exit_signal_time"] == at(DAY, "09:16").isoformat()
    assert t["exit_reason"] == "trailing_stop"
    assert t["exit_price"] == pytest.approx(104.99)
    assert t["stop_price"] == pytest.approx(99.01)
    assert t["trailing_exit"]["stop_price"] == 102
    assert t["trailing_exit"]["best_price"] == 103  # Future extrema / unfilled bars cannot revise the pending exit.
    assert any(e["action"] == "fill_unavailable" for e in result["events"])
