import copy
import csv
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
import talib

from research.calendar import MINUTE, at
from research.config import ResearchError, changed
from research.data import Dataset, load_data
from research.execution import (
    ContractState,
    PortfolioBacktest,
    Position,
    RiskAllocator,
    can_fill,
    fee,
    protective_touch,
)
from research.experiments import (
    calibrate_ticks,
    development_window,
    experiment_budget,
    walk_forward,
)
from research.fixtures import create_fixture
from research.signals import (
    Features,
    SignalLogic,
    exit_flags,
    feature_frame,
    pullback_event,
    rank_candidates,
)


@pytest.fixture(scope="module")
def research_data(tmp_path_factory):
    cfg = create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY"))
    data = load_data(cfg)
    assert not data.quality["errors"]
    return data


@pytest.fixture(scope="module")
def features(research_data):
    return Features(research_data)


DAY = "2026-01-08"
KEY = "aa2603.SHFE"


def single(data, overrides=None):
    cfg = copy.deepcopy(data.cfg)
    cfg["metadata"]["contracts"] = [
        m for m in cfg["metadata"]["contracts"] if m["symbol"] == "aa2603"
    ]
    if overrides:
        cfg["strategy"].update(overrides)
    return Dataset([b for b in data.bars if b.key == KEY], cfg)


def test_group_cutoffs_eighth_minute_and_insufficient_k(research_data):
    pool, _ = research_data.pool(DAY)
    for clock, expected in [
        ("09:07", set()),
        ("09:08", {"commodity"}),
        ("09:37", {"commodity"}),
        ("09:38", {"commodity", "financial"}),
    ]:
        rows, _ = rank_candidates(research_data, DAY, pool, at(DAY, clock), 10)
        assert {r["group"] for r in rows} == expected
        assert all(r["selected"] for r in rows)
    rows, _ = rank_candidates(research_data, DAY, pool, at(DAY, "09:38"), "ALL")
    assert all(r["r8"] > 0 if r["direction"] == "LONG" else r["r8"] < 0 for r in rows)


def duplicated_contract(data, product="ee", symbol="ee2603"):
    cfg = copy.deepcopy(data.cfg)
    original = next(m for m in cfg["metadata"]["contracts"] if m["symbol"] == "aa2603")
    extra = copy.deepcopy(original)
    extra.update(product=product, symbol=symbol)
    cfg["metadata"]["contracts"].append(extra)
    cfg["strategy"]["fixed_ticks"][product] = copy.deepcopy(
        cfg["strategy"]["fixed_ticks"]["aa"]
    )
    bars = list(data.bars) + [
        replace(b, symbol=symbol, product=product) for b in data.bars if b.key == KEY
    ]
    return Dataset(bars, cfg)


def test_ties_candidate_lock_and_no_replacement(research_data):
    data = duplicated_contract(research_data)
    pool, _ = data.pool(DAY)
    candidates, _ = rank_candidates(data, DAY, pool, at(DAY, "09:08"), 1)
    longs = [r for r in candidates if r["direction"] == "LONG"]
    assert [r["contract"] for r in longs] == [KEY, "ee2603.SHFE"]
    assert [r["selected"] for r in longs] == [True, False]
    selected = dict(longs[0])
    bars = [
        replace(b, open_interest=0)
        if b.key == KEY and b.trading_day == DAY and b.datetime >= at(DAY, "09:08")
        else b
        for b in data.bars
    ]
    changed_data = Dataset(bars, data.cfg)
    bars_at = changed_data.by_day[(DAY, KEY)]
    check = SignalLogic(changed_data, Features(changed_data)).evaluate(
        bars_at[at(DAY, "09:20")], selected
    )
    assert not check["filters"]["oi"]
    assert not longs[1][
        "selected"
    ]  # No signal filter can rewrite the frozen candidates.


def test_real_contract_choice_uses_previous_day_not_future_oi(research_data):
    data = duplicated_contract(research_data, product="aa", symbol="aa2604")
    bars = [
        replace(b, open_interest=b.open_interest / 2 if b.trading_day < DAY else 1e9)
        if b.symbol == "aa2604"
        else b
        for b in data.bars
    ]
    data = Dataset(bars, data.cfg)
    pool, _ = data.pool(DAY)
    assert next(r for r in pool if r["product"] == "aa")["contract"] == KEY
    assert (
        next(r for r in pool if r["product"] == "aa")["selection_day"] == "2026-01-07"
    )
    # A missing immediately preceding trading day cannot be replaced by an older day.
    data = Dataset([b for b in bars if b.trading_day != "2026-01-07"], data.cfg)
    pool, exclusions = data.pool(DAY)
    assert not pool
    assert any(r["reason"] == "previous_trading_day_incomplete" for r in exclusions)


def test_no_partial_higher_bars_or_cross_break_aggregation(research_data, features):
    fifteen = features.latest(KEY, 15, at(DAY, "09:14"))
    assert str(fifteen.day) < DAY
    assert str(features.latest(KEY, 15, at(DAY, "09:15")).day) == DAY
    five = features.latest(KEY, 5, at(DAY, "09:04"))
    assert str(five.day) < DAY
    assert features.latest(KEY, 5, at(DAY, "09:05")).name == pd.Timestamp(
        at(DAY, "09:05")
    )
    for minutes in (5, 15):
        frame = features.frames[(KEY, minutes)]
        for end in frame.index:
            assert end.strftime("%H:%M") not in {
                "10:20",
                "10:25",
                "11:35",
                "12:00",
                "13:00",
            }
    missing = Dataset(
        [
            b
            for b in research_data.bars
            if not (b.key == KEY and b.datetime == at(DAY, "09:03"))
        ],
        research_data.cfg,
    )
    f = Features(missing)
    assert str(f.latest(KEY, 5, at(DAY, "09:05")).day) < DAY
    assert str(f.latest(KEY, 15, at(DAY, "09:15")).day) < DAY


def test_atr_is_talib_wilder_and_current_volume_excluded():
    rows = [
        {
            "end": at(DAY, "09:00") + (i + 1) * MINUTE,
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100 + i,
            "volume": 100 if i < 25 else 10000,
        }
        for i in range(26)
    ]
    frame = feature_frame(rows, 14)
    expected = talib.ATR(
        frame.high.to_numpy(float),
        frame.low.to_numpy(float),
        frame.close.to_numpy(float),
        timeperiod=14,
    )
    np.testing.assert_allclose(frame.atr, expected, equal_nan=True)
    assert frame.iloc[-1].volume_baseline == 100
    assert frame.iloc[-1].vr == 100
    assert frame.iloc[-1].previous_atr == frame.iloc[-2].atr
    zero = feature_frame([{**r, "volume": 0} for r in rows], 14)
    assert pd.isna(zero.iloc[-1].vr)


def test_short_requires_increasing_oi_and_current_15m(research_data, features):
    pool, _ = research_data.pool(DAY)
    candidates, _ = rank_candidates(research_data, DAY, pool, at(DAY, "09:38"), 1)
    candidate = next(r for r in candidates if r["contract"] == "bb2603.SHFE")
    b = research_data.by_day[(DAY, candidate["contract"])][at(DAY, "09:20")]
    logic = SignalLogic(research_data, features)
    good = logic.evaluate(b, candidate)
    assert good["filters"]["oi"]
    assert good["filters"]["trend_15m"]
    bad = logic.evaluate(replace(b, open_interest=9000), candidate)
    assert not bad["filters"]["oi"]
    before = research_data.by_day[(DAY, candidate["contract"])][at(DAY, "09:13")]
    assert not logic.evaluate(before, candidate)["filters"]["current_session_15m"]


def test_session_vwap_continuity_excludes_recess_and_missing_actual_minute(
    research_data, features
):
    pool, _ = research_data.pool(DAY)
    candidates, _ = rank_candidates(research_data, DAY, pool, at(DAY, "09:08"), 1)
    candidate = next(r for r in candidates if r["contract"] == KEY)
    bar = research_data.by_day[(DAY, KEY)][at(DAY, "13:35")]
    result = SignalLogic(research_data, features).evaluate(bar, candidate)
    assert result["filters"]["session_data_continuous"] and result["filters"]["vwap"]
    missing = Dataset(
        [
            b
            for b in research_data.bars
            if not (b.key == KEY and b.datetime == at(DAY, "10:31"))
        ],
        research_data.cfg,
    )
    result = SignalLogic(missing, Features(missing)).evaluate(bar, candidate)
    assert (
        not result["filters"]["session_data_continuous"]
        and not result["filters"]["vwap"]
    )


def pullback_frame(line, sign=1, dual=False):
    rows = [
        {
            "close": 102,
            "high": 102.5,
            "low": 101,
            "ma10": 100,
            "ma20": 98,
            "previous_atr": 1,
        }
        for _ in range(5)
    ]
    rows[3]["low"] = 100 if line == 10 else 98
    rows[4]["close"] = 103
    if dual:
        rows[3]["ma20"] = 100
    frame = pd.DataFrame(
        rows, index=pd.date_range(at(DAY, "09:15"), periods=5, freq="min")
    )
    if sign < 0:
        for c in ("close", "ma10", "ma20"):
            frame[c] = 200 - frame[c]
        high, low = 200 - frame.low, 200 - frame.high
        frame["high"], frame["low"] = high, low
    return frame


@pytest.mark.parametrize("line", [10, 20])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_both_pullback_references_and_short_symmetry(research_data, line, direction):
    frame = pullback_frame(line, 1 if direction == "LONG" else -1)
    event = pullback_event(
        frame, direction, f"pullback_ma{line}", 0.01, research_data.cfg
    )
    assert event and event["references"] == [line]
    assert not pullback_event(
        frame, direction, f"pullback_ma{30 - line}", 0.01, research_data.cfg
    )
    assert pullback_event(frame, direction, "pullback_either", 0.01, research_data.cfg)


def test_dual_touch_produces_one_event(research_data):
    event = pullback_event(
        pullback_frame(10, dual=True),
        "LONG",
        "pullback_either",
        0.01,
        research_data.cfg,
    )
    assert event["dual_touch"] and event["references"] == [10, 20]


@pytest.mark.parametrize("line", [10, 20])
def test_pullback_can_generate_real_unmocked_strategy_fill(research_data, line):
    data = single(research_data, {"entry_mode": f"pullback_ma{line}"})
    features = Features(data)
    touch = at(DAY, "09:19")
    confirmation = at(DAY, "09:20")
    reference = features.latest(KEY, 1, touch + MINUTE)[f"ma{line}"]
    rows = []
    for b in data.bars:
        if b.datetime == touch:
            b = replace(b, low=round(float(reference), 2))
        elif b.datetime == confirmation:
            close = round(b.close + 0.35, 2)
            b = replace(b, close=close, high=round(close + 0.3, 2))
        rows.append(b)
    data = Dataset(rows, data.cfg)
    result = PortfolioBacktest(data).run(DAY, DAY)
    trigger = next(r for r in result["signals"] if r["trigger"])
    assert trigger["time"] == at(DAY, "09:21").isoformat()
    assert trigger["pullback"]["references"] == [line]
    assert trigger["all_pass"] and trigger["risk_pass"] and trigger["filled"]
    assert result["trades"][0]["entry_time"] == at(DAY, "09:21").isoformat()


def test_ma10_ma20_vwap_oi_are_not_holding_exits(research_data):
    one = pd.Series(
        {
            "close": 101,
            "ma10": 105,
            "ma20": 104,
            "ma40": 100,
            "previous_atr": 2,
            "vr": 1,
        }
    )
    past = pd.DataFrame({"close": [102, 103, 101]})
    flags, _ = exit_flags(one, past, "LONG", research_data.cfg)
    assert flags == []
    one["vr"] = 3
    one["ma40"] = 102
    flags, _ = exit_flags(one, past, "LONG", research_data.cfg)
    assert flags == ["volume", "ma40_cross"]
    one["vr"] = float("nan")
    flags, diag = exit_flags(one, past, "LONG", research_data.cfg)
    assert flags == ["ma40_cross"] and not diag["vr_valid"]


def test_ma40_approach_is_explicit_and_symmetric(research_data):
    cfg = changed(research_data.cfg, ma40_mode="approach")
    one = pd.Series({"close": 100.1, "ma40": 100, "previous_atr": 1, "vr": 1})
    flags, _ = exit_flags(
        one, pd.DataFrame({"close": [100.3, 100.2, 100.1]}), "LONG", cfg
    )
    assert flags == ["ma40_approach"]
    one.close = 99.9
    flags, _ = exit_flags(
        one, pd.DataFrame({"close": [99.7, 99.8, 99.9]}), "SHORT", cfg
    )
    assert flags == ["ma40_approach"]


def make_position(data, opened=None):
    meta = data.metadata.get(KEY, DAY)
    signal = {
        "time": at(DAY, "09:14").isoformat(),
        "rank": 1,
        "r8": 0.01,
        "pullback": None,
        "snapshot": {},
    }
    return Position(
        KEY,
        meta,
        1,
        1,
        100,
        100,
        opened or at(DAY, "09:15"),
        DAY,
        99,
        102,
        1,
        12,
        100,
        signal,
    )


def test_ambiguous_stops_gaps_and_pre_entry_prices(research_data):
    p = make_position(research_data)
    b = research_data.by_day[(DAY, KEY)][at(DAY, "09:15")]
    ambiguous = replace(b, open=100, high=103, low=98, close=101)
    hit = protective_touch(p, ambiguous)
    assert hit["reason"] == "fixed_stop" and hit["ambiguous"] and hit["raw_price"] == 99
    gap = replace(ambiguous, open=97, low=96, close=98)
    hit = protective_touch(p, gap)
    assert hit["raw_price"] == 97 and hit["gap"]
    # Previous candle can cross both barriers; only the post-entry bar is considered.
    after = replace(b, open=100, high=101, low=99.5, close=100)
    assert protective_touch(p, after) is None


def test_next_open_fill_and_actual_fixed_ticks_and_single_pullback_event(
    research_data, monkeypatch
):
    data = single(research_data, {"entry_mode": "pullback_either"})
    engine = PortfolioBacktest(data)
    original = engine.logic.evaluate

    def inject(bar, candidate, state_allows=True, before_cutoff=True):
        evaluation = original(bar, candidate, state_allows, before_cutoff)
        evaluation.update(
            all_pass=state_allows
            and before_cutoff
            and bar.datetime >= at(DAY, "09:15"),
            pullback={
                "event": "one_deterministic_event",
                "references": [10, 20],
                "dual_touch": True,
            },
        )
        return evaluation

    monkeypatch.setattr(engine.logic, "evaluate", inject)
    result = engine.run(DAY, DAY)
    entries = [e for e in result["events"] if e["action"] == "entry_filled"]
    assert len(entries) == 1
    assert entries[0]["time"] == at(DAY, "09:16").isoformat()
    assert entries[0]["stop"] == pytest.approx(entries[0]["price"] - 2)
    assert entries[0]["target"] == pytest.approx(entries[0]["price"] + 4)
    trade = result["trades"][0]
    assert trade["entry_signal_time"] == at(DAY, "09:16").isoformat()
    assert trade["entry_price"] == pytest.approx(
        data.by_day[(DAY, KEY)][at(DAY, "09:16")].open + 0.01
    )
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["fees"])


def test_cutoff_cancels_pending_and_force_timer_without_bar_preserves_risk(
    research_data,
):
    data = single(research_data)
    engine = PortfolioBacktest(data)
    meta = data.metadata.get(KEY, DAY)
    st = engine.state(KEY)
    st.name, st.pending = (
        "ENTRY_PENDING",
        {"signal": {"time": at(DAY, "14:29").isoformat()}},
    )
    engine.timer(at(DAY, "14:30"), DAY, {KEY: meta})
    assert st.name == "FLAT" and st.pending is None
    assert engine.events[-1]["action"] == "entry_cancelled"
    st.name, st.position = "LONG", make_position(data)
    engine.timer(at(DAY, "14:50"), DAY, {KEY: meta})
    assert st.name == "EXIT_PENDING" and st.position is not None
    engine.fill_open(KEY, None, at(DAY, "14:50"))
    assert not engine.trades and st.position is not None
    bar = replace(
        data.by_day[(DAY, KEY)][at(DAY, "14:51")],
        open=101,
        high=101,
        low=101,
        close=101,
        limit_up=101,
    )
    engine.fill_open(KEY, bar, bar.datetime)
    assert not engine.trades and st.name == "EXIT_PENDING"
    bar = replace(bar, high=102)
    engine.fill_open(KEY, bar, bar.datetime)
    assert st.position is None and engine.trades[0]["exit_reason"] == "time_force"
    assert engine.trades[0]["exit_time"] == at(DAY, "14:51").isoformat()


def test_full_session_missing_tail_never_fakes_flatten(research_data, monkeypatch):
    data = single(research_data)
    data = Dataset(
        [
            b
            for b in data.bars
            if not (b.trading_day == DAY and b.datetime >= at(DAY, "14:49"))
        ],
        data.cfg,
    )
    data.cfg["strategy"]["fixed_ticks"]["aa"] = {
        "stop_loss_ticks": 100000,
        "take_profit_ticks": 100000,
    }
    data.cfg["strategy"]["enable_volume_exit"] = False
    engine = PortfolioBacktest(data)
    engine.cash = 1e9
    engine.cfg["risk"].update(
        initial_capital=1e9, trade_risk_fraction=0.01, portfolio_risk_fraction=0.1
    )
    result = engine.run(DAY, DAY)
    assert result["status"] == "execution_risk"
    assert result["open_positions"] and result["unflattened_risk"]
    assert not result["trades"]
    assert any(
        e["action"] == "exit_requested" and e["time"] == at(DAY, "14:50").isoformat()
        for e in result["events"]
    )


def test_no_hold_across_break_exits_before_recess_and_records_unfilled_risk(
    research_data,
):
    data = single(research_data, {"allow_hold_across_break": False})
    engine = PortfolioBacktest(data)
    meta = data.metadata.get(KEY, DAY)
    st = engine.state(KEY)
    st.name, st.position = "LONG", make_position(data)
    engine.timer(at(DAY, "10:14"), DAY, {KEY: meta})
    assert st.name == "EXIT_PENDING" and st.pending["flags"] == ["break_close"]
    bar = data.by_day[(DAY, KEY)][at(DAY, "10:14")]
    engine.fill_open(KEY, bar, bar.datetime)
    assert engine.trades[0]["exit_time"] == at(DAY, "10:14").isoformat()
    assert engine.trades[0]["exit_reason"] == "break_close"
    st.name, st.position = "LONG", make_position(data)
    engine.timer(at(DAY, "10:14"), DAY, {KEY: meta})
    engine.fill_open(KEY, None, at(DAY, "10:14"))
    engine.timer(at(DAY, "10:15"), DAY, {KEY: meta})
    assert st.position and engine.break_risk


def test_zero_volume_missing_and_break_and_early_close(research_data):
    data = single(research_data)
    meta = data.metadata.get(KEY, DAY)
    b = data.by_day[(DAY, KEY)][at(DAY, "09:15")]
    assert not can_fill(replace(b, volume=0))[0]
    assert not can_fill(None)[0]
    assert data.calendar.locate(at(DAY, "10:20"), DAY, meta) is None
    data.calendar.cfg["overrides"][DAY] = {
        "commodity": {"sessions": [["09:00", "10:15"]]}
    }
    cutoff, force, close = data.calendar.deadlines(
        DAY, meta, data.cfg["strategy"]["times"]
    )
    assert cutoff == force == at(DAY, "10:14") and close == at(DAY, "10:15")


def test_fee_modes_close_today_close_yesterday_no_double_slippage(research_data):
    meta = research_data.metadata.get(KEY, DAY)
    assert fee(meta, DAY, "open", 100, 3) == 3
    assert fee(meta, DAY, "close_today", 100, 3) == 6
    assert fee(meta, DAY, "close_yesterday", 100, 3) == pytest.approx(0.3)
    changed_meta = copy.deepcopy(meta)
    changed_meta["fees"].append(
        {
            "effective_from": DAY,
            "open": {"mode": "fixed", "value": 7},
            "close_today": {"mode": "fixed", "value": 8},
            "close_yesterday": {"mode": "fixed", "value": 9},
        }
    )
    assert fee(changed_meta, DAY, "open", 100, 2) == 14
    assert fee(changed_meta, "2026-01-07", "open", 100, 2) == 2


def test_top_k_never_increases_budget_and_one_lot_too_expensive_skipped(research_data):
    cfg = copy.deepcopy(research_data.cfg)
    meta = research_data.metadata.get(KEY, DAY)
    allocations = [
        RiskAllocator(changed(cfg, k=k)).allocate(meta, 100, DAY, {}, 1e6)
        for k in [1, 2, 3, 4, 5, 8, 10, "ALL"]
    ]
    assert all(a == allocations[0] for a in allocations)
    cfg["risk"]["trade_risk_fraction"] = 1e-10
    assert RiskAllocator(cfg).allocate(meta, 100, DAY, {}, 1e6)[0] == 0
    cfg = copy.deepcopy(research_data.cfg)
    cfg["risk"]["max_positions"] = 1
    states = {KEY: ContractState(name="LONG", position=make_position(research_data))}
    assert (
        "max_positions" in RiskAllocator(cfg).allocate(meta, 100, DAY, states, 1e6)[3]
    )


def test_roll_uses_new_contract_history_only(research_data):
    data = duplicated_contract(research_data, product="aa", symbol="aa2604")
    rows = [
        replace(
            b,
            open=b.open * 2,
            high=b.high * 2,
            low=b.low * 2,
            close=b.close * 2,
            open_interest=50000,
        )
        if b.symbol == "aa2604"
        else b
        for b in data.bars
        if b.symbol != "aa2604" or b.trading_day >= "2026-01-07"
    ]
    data = Dataset(rows, data.cfg)
    pool, _ = data.pool(DAY)
    assert next(r for r in pool if r["product"] == "aa")["contract"] == "aa2604.SHFE"
    features = Features(data)
    assert pd.isna(features.latest("aa2604.SHFE", 15, at(DAY, "09:15")).ma20)
    assert not pd.isna(features.latest(KEY, 15, at(DAY, "09:15")).ma20)


def test_direct_transition_not_repeated_each_minute(research_data):
    data = single(research_data)
    data.cfg["strategy"]["fixed_ticks"]["aa"] = {
        "stop_loss_ticks": 5000,
        "take_profit_ticks": 5000,
    }
    data.cfg["strategy"]["enable_volume_exit"] = False
    engine = PortfolioBacktest(data)
    result = engine.run(DAY, DAY)
    requested = [r for r in result["events"] if r["action"] == "entry_requested"]
    assert len(requested) == 1
    assert requested[0]["time"] == at(DAY, "09:15").isoformat()
    assert result["trades"][0]["exit_reason"] == "time_force"
    assert not result["open_positions"]


def test_gap_fill_uses_open_with_slippage_and_ambiguous_counter(research_data):
    data = single(research_data)
    engine = PortfolioBacktest(data)
    st = engine.state(KEY)
    st.position, st.name = make_position(data), "LONG"
    bar = replace(
        data.by_day[(DAY, KEY)][at(DAY, "09:16")], open=97, high=98, low=96, close=97
    )
    hit = protective_touch(st.position, bar)
    engine.close(
        KEY,
        bar,
        hit["raw_price"],
        bar.datetime,
        hit["flags"],
        hit["ambiguous"],
        hit["gap"],
    )
    assert engine.trades[0]["exit_price"] == pytest.approx(96.99)
    assert engine.trades[0]["net_pnl"] == pytest.approx(-33.1)
    st.position, st.name = make_position(data), "LONG"
    bar = replace(bar, open=100, high=103, low=98, close=101)
    hit = protective_touch(st.position, bar)
    engine.close(
        KEY, bar, hit["raw_price"], bar.end, hit["flags"], hit["ambiguous"], hit["gap"]
    )
    assert engine.trades[-1]["exit_price"] == pytest.approx(98.99)
    assert engine.trades[-1]["exit_flags"] == ["fixed_stop", "fixed_target"]
    assert engine.ambiguities == 1


@pytest.mark.parametrize("sign", [1, -1], ids=["long", "short"])
@pytest.mark.parametrize("reason", ["fixed_stop", "fixed_target"])
def test_opening_gap_exit_ignores_later_opposite_touch(research_data, sign, reason):
    data = single(research_data)
    engine = PortfolioBacktest(data)
    position = replace(
        make_position(data), sign=sign, stop=100 - sign, target=100 + sign * 2
    )
    opening = position.stop - sign if reason == "fixed_stop" else position.target + sign
    base = data.by_day[(DAY, KEY)][at(DAY, "09:16")]
    first = replace(base, open=opening, high=opening, low=opening, close=opening)
    later = replace(
        first,
        high=max(position.stop, position.target, opening) + 1,
        low=min(position.stop, position.target, opening) - 1,
    )
    hit = protective_touch(position, later)
    assert hit == protective_touch(position, first)
    st = engine.state(KEY)
    st.position, st.name = position, "LONG" if sign == 1 else "SHORT"
    assert engine.close(
        KEY, later, hit["raw_price"], later.datetime,
        hit["flags"], hit["ambiguous"], hit["gap"],
    )
    trade = engine.trades[-1]
    assert trade["exit_reason"] == reason and trade["exit_flags"] == [reason]
    assert trade["exit_price"] == pytest.approx(opening - sign * 0.01)
    assert trade["exit_time"] == later.datetime.isoformat()
    assert trade["gap"] and not trade["ambiguous_bar"] and engine.ambiguities == 0


def test_finished_indicator_cache_matches_uncached_causal_values(
    research_data, tmp_path
):
    f1 = Features(research_data, tmp_path)
    f2 = Features(research_data, tmp_path)
    assert f1.cache_key == f2.cache_key
    for key in research_data.by_contract:
        for period in (1, 5, 15):
            first = f1.latest(key, period, at(DAY, "13:35"))
            second = f2.latest(key, period, at(DAY, "13:35"))
            for field in (
                "close",
                "ma10",
                "ma20",
                "ma40",
                "atr",
                "previous_atr",
                "shock",
                "efficiency",
                "vr",
            ):
                assert first[field] == pytest.approx(second[field], nan_ok=True)


def test_future_changes_do_not_change_past_candidates_features_signals_or_fills(
    research_data,
):
    data = single(research_data)
    cutoff = at(DAY, "11:00")
    changed_bars = [
        replace(
            b,
            open=b.open * 1.1,
            high=b.high * 1.1,
            low=b.low * 1.1,
            close=b.close * 1.1,
            open_interest=1e9,
            volume=1e6,
        )
        if b.datetime >= cutoff
        else b
        for b in data.bars
    ]
    other = Dataset(changed_bars, copy.deepcopy(data.cfg))
    f1, f2 = Features(data), Features(other)
    for minutes in (1, 5, 15):
        pd.testing.assert_frame_equal(
            f1.frames[(KEY, minutes)].loc[: cutoff - MINUTE],
            f2.frames[(KEY, minutes)].loc[: cutoff - MINUTE],
        )
    a = PortfolioBacktest(data).run(DAY, DAY)
    b = PortfolioBacktest(other).run(DAY, DAY)
    assert a["daily_candidates"] == b["daily_candidates"]
    for field, timefield in [
        ("signals", "time"),
        ("events", "time"),
        ("orders", "time"),
    ]:
        left = [r for r in a[field] if r[timefield] < cutoff.isoformat()]
        right = [r for r in b[field] if r[timefield] < cutoff.isoformat()]
        assert left == right


def test_optimizer_cannot_access_locked_test_and_calibration_is_training_only(
    research_data, tmp_path
):
    cfg = research_data.cfg
    with pytest.raises(ResearchError, match="锁定测试"):
        development_window(cfg, cfg["splits"]["test"])
    with pytest.raises(ResearchError, match="物理截断"):
        calibrate_ticks(research_data, cfg)
    training = research_data.until(cfg["splits"]["train"]["end"])
    assert max(b.trading_day for b in training.bars) < cfg["splits"]["test"]["start"]
    candidates = calibrate_ticks(training, cfg)
    assert candidates["locked_test_read"] is False
    mutated = Dataset(
        [
            replace(b, close=1e9, high=1e9)
            if b.trading_day == cfg["splits"]["test"]["start"]
            else b
            for b in research_data.bars
        ],
        cfg,
    )
    assert (
        calibrate_ticks(mutated.until(cfg["splits"]["train"]["end"]), cfg) == candidates
    )
    bad = copy.deepcopy(cfg)
    bad["experiments"]["windows"] = [
        {"train": cfg["splits"]["train"], "validation": cfg["splits"]["test"]}
    ]
    with pytest.raises(ResearchError, match="锁定测试"):
        walk_forward(research_data, bad, tmp_path)


def test_csv_duplicate_oi_missing_end_timestamp_and_cumulative_reset(
    research_data, tmp_path
):
    cfg = copy.deepcopy(research_data.cfg)
    source = tmp_path / "input.csv"
    cfg["data"]["sources"] = [
        {"path": str(source), "format": "csv", "provenance": "SYNTHETIC_TEST_ONLY"}
    ]
    bars = [
        research_data.by_day[(DAY, KEY)][at(DAY, "09:00") + i * MINUTE]
        for i in range(4)
    ]

    def save(rows):
        with source.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    rows = [b.wire() for b in bars]
    save(rows + [rows[0]])
    assert any("重复" in e["reason"] for e in load_data(cfg).quality["errors"])
    rows = [
        dict(r, datetime=(b.datetime + MINUTE).isoformat(), open_interest="")
        for r, b in zip(rows, bars, strict=True)
    ]
    cfg["data"]["timestamp"] = "end"
    save(rows)
    loaded = load_data(cfg)
    assert (
        loaded.bars[0].datetime == bars[0].datetime
        and loaded.bars[0].open_interest is None
    )
    cfg["data"].update(
        timestamp="start",
        counter_mode="cumulative",
        cumulative_first_is_zero_based=True,
    )
    rows = [
        dict(b.wire(), volume=v, turnover=v * 1000)
        for b, v in zip(bars, [100, 300, 50, 150], strict=True)
    ]
    save(rows)
    loaded = load_data(cfg)
    assert [b.volume for b in loaded.bars] == [100, 200, 100]
    assert any(
        r["reason"] == "cumulative_reset_or_gap_unknown_increment"
        for r in loaded.quality["excluded_rows"]
    )


def test_missing_fixed_ticks_blocks_formal_backtest(research_data):
    data = single(research_data)
    data.cfg["strategy"]["fixed_ticks"]["aa"]["stop_loss_ticks"] = None
    with pytest.raises(ResearchError, match="固定止损"):
        PortfolioBacktest(data).run(DAY, DAY)


@pytest.mark.parametrize("budget", [0, -1, 1.5])
def test_invalid_experiment_budget_never_expands_to_default(research_data, budget):
    with pytest.raises(ResearchError, match="预算"):
        experiment_budget(research_data.cfg, budget)
    assert experiment_budget(research_data.cfg, 1000) == 32


def test_walk_forward_never_marks_synthetic_runs_as_selection_evidence(
    research_data, tmp_path
):
    cfg = copy.deepcopy(research_data.cfg)
    cfg["experiments"]["minimum_validation_days"] = 1
    cfg["experiments"]["minimum_trades_for_selection"] = 0
    data = research_data.until(cfg["splits"]["validation"]["end"])
    result = walk_forward(data, cfg, tmp_path, budget=1)
    assert result["folds"][0]["results"][0]["status"] == "completed"
    assert result["eligible_runs"] == []
    assert result["selection"] is None and result["locked_test_read"] is False


def test_direct_entry_becomes_eligible_when_portfolio_capacity_frees(research_data):
    cfg = copy.deepcopy(research_data.cfg)
    cfg["metadata"]["contracts"] = [
        m for m in cfg["metadata"]["contracts"] if m["group"] == "commodity"
    ]
    cfg["risk"]["max_positions"] = 1
    next(m for m in cfg["metadata"]["contracts"] if m["product"] == "bb")[
        "time_profile"
    ] = "early_test_exit"
    cfg["strategy"]["times"]["early_test_exit"] = {
        "entry_cutoff": "09:16",
        "force_close": "09:16",
    }
    data = Dataset([b for b in research_data.bars if b.exchange == "SHFE"], cfg)
    result = PortfolioBacktest(data).run(DAY, DAY)
    rejected = next(
        r
        for r in result["signals"]
        if r["contract"] == KEY and r["time"] == at(DAY, "09:15").isoformat()
    )
    assert rejected["all_pass"] and rejected["trigger"]
    assert "max_positions" in rejected["risk_rejections"]
    assert rejected["qualified_with_risk"] is False
    admitted = next(
        r
        for r in result["signals"]
        if r["contract"] == KEY and r["time"] == at(DAY, "09:17").isoformat()
    )
    assert admitted["qualified_with_risk"] and admitted["filled"]
    assert any(
        r["contract"] == KEY and r["entry_time"] == at(DAY, "09:17").isoformat()
        for r in result["trades"]
    )


def test_direct_retries_after_gap_margin_recheck_cancels_entry(research_data):
    data = single(research_data)
    opening = at(DAY, "09:15")
    original = data.by_day[(DAY, KEY)][opening]
    data.cfg["risk"].update(
        initial_capital=1000,
        trade_risk_fraction=0.05,
        portfolio_risk_fraction=0.1,
        margin_fraction=(original.open + 0.15) / 1000,
        max_lots_per_contract=1,
        group_fractions={"commodity": 1.0, "financial": 0.0},
    )
    # A modest opening gap exceeds the reserved margin while the completed
    # candle keeps every original price filter unchanged and passing.
    bars = [
        replace(b, open=b.open + 0.2) if b.datetime == opening else b for b in data.bars
    ]
    result = PortfolioBacktest(Dataset(bars, data.cfg)).run(DAY, DAY)
    assert any(
        e["action"] == "entry_cancelled"
        and e.get("reason") == "fill_risk_recheck"
        and e["time"] == opening.isoformat()
        for e in result["events"]
    )
    assert any(
        e["action"] == "entry_filled" and e["time"] == (opening + MINUTE).isoformat()
        for e in result["events"]
    )


def test_simultaneous_open_fills_do_not_read_other_contract_intraminute_stop(
    research_data,
):
    cfg = copy.deepcopy(research_data.cfg)
    cfg["metadata"]["contracts"] = [
        m for m in cfg["metadata"]["contracts"] if m["group"] == "commodity"
    ]
    cfg["risk"].update(
        initial_capital=1000,
        trade_risk_fraction=0.0235,
        portfolio_risk_fraction=0.1,
        margin_fraction=1.0,
        max_lots_per_contract=1,
        group_fractions={"commodity": 1.0, "financial": 0.0},
    )
    opening = at(DAY, "09:15")
    bars = [b for b in research_data.bars if b.exchange == "SHFE"]
    changed_bars = [
        replace(b, low=b.open - 3) if b.key == KEY and b.datetime == opening else b
        for b in bars
    ]
    before = PortfolioBacktest(Dataset(bars, cfg)).run(DAY, DAY)
    after = PortfolioBacktest(Dataset(changed_bars, cfg)).run(DAY, DAY)
    for result in (before, after):
        assert any(
            e["action"] == "entry_filled"
            and e["contract"] == "bb2603.SHFE"
            and e["time"] == opening.isoformat()
            for e in result["events"]
        )

    # OHLC changed after this shared opening, so every earlier fill stays identical.
    def select(result):
        return [e for e in result["events"] if e["time"] <= opening.isoformat()]

    assert select(before) == select(after)


def test_offline_package_does_not_import_ctp_in_fresh_process():
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import research.__main__; assert 'vnpy_ctp' not in sys.modules",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
