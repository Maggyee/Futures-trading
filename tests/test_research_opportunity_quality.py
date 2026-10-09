"""Causal cohorts, paired controls, trading-minute labels and affordability."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from research.calendar import MINUTE, at
from research.data import Bar, Dataset, load_data
from research.execution import RiskAllocator
from research.fixtures import create_fixture
from research.opportunity_quality import (
    LabelStudy,
    PullbackLabels,
    match_observations,
    merge_segments,
)
from research.opportunity_quality_assessment import daily_values, estimate
from research.signals import Features

DAY, KEY = "2026-01-08", "aa2603.SHFE"
LABEL_PLAN = {"forward_labels": {"horizons": [5, 15, 30]},
              "observation": {"grid_minutes": 5}, "pullback": {"wait_trading_minutes": 10}}
MATCH = {"comparisons": [["rank_1_2", "rank_6_plus"]],
         "maximum_liquidity_ratio": 4, "maximum_relative_atr_ratio": 2}


def bar(clock, close, low=None, high=None):
    return Bar(at(DAY, clock), DAY, "SHFE", "aa2603", "aa", close,
               close + .1 if high is None else high, close - .1 if low is None else low,
               close, 10, 100)


def row(identity="a", minute=15, rank=1, **extra):
    return {"id": identity, "date": DAY, "contract": identity + ".SHFE", "direction": "LONG",
            "time": (at(DAY, "09:00") + minute * MINUTE).isoformat(), "minute_index": minute,
            "period": at(DAY, "09:00").isoformat(), "group": "commodity", "session_profile": "commodity",
            "previous_volume": 100, "relative_atr": .01, "rank": rank,
            "rank_group": "rank_1_2" if rank <= 2 else "rank_6_plus", "representative": True, **extra}


def test_segment_does_not_reselect_later_filter_pass():
    rows = [row("a0", 15, contract=KEY, conditions={"oi": False}),
            row("a1", 20, contract=KEY, conditions={"oi": True}),
            row("a2", 50, contract=KEY), row("a3", 85, contract=KEY),
            row("a4", 90, contract=KEY, period=at(DAY, "10:00").isoformat())]
    merge_segments(rows)
    assert [r["representative"] for r in rows] == [True, False, False, True, True]
    assert rows[1]["segment_id"] == rows[2]["segment_id"] == "a0"
    assert not rows[0]["conditions"]["oi"]


def test_matching_ignores_all_future_labels_and_requires_same_clock_direction():
    rows = [row("top"), row("z_control", rank=6), row("a_control", rank=7),
            row("opposite", rank=8, direction="SHORT"), row("late", minute=20, rank=9),
            row("liquid", rank=10, previous_volume=10000)]
    expected = match_observations(rows, MATCH)
    assert len(expected) == 1 and expected[0]["control"] == "a_control"
    for r in rows:
        r["labels"] = {"15": {"net_atr": -999 if r["id"] == "a_control" else 999}}
    assert match_observations(rows, MATCH) == expected


def test_control_not_reused_at_same_clock():
    pairs = match_observations([row("top1"), row("top2", rank=2), row("only_control", rank=6)], MATCH)
    assert len(pairs) == 1


def label_study(direction="LONG", schedule=None):
    schedule = schedule or [at(DAY, "09:15") + i * MINUTE for i in range(30)]
    sign = 1 if direction == "LONG" else -1
    bars = [replace(bar(t.strftime("%H:%M"), 100 + sign * i), open=100 + sign * i)
            for i, t in enumerate(schedule)]
    meta = {"symbol": "aa2603", "exchange": "SHFE", "product": "aa", "group": "commodity",
            "tick_size": 1., "value_per_price": 10., "margin_rate": .1,
            "fees": [{"effective_from": DAY, "open": {"mode": "fixed", "value": 2},
                      "close_today": {"mode": "fixed", "value": 3}}]}
    cfg = {"strategy": {"slippage_ticks": 1, "extension_max": 1.5,
                        "entry_cost_filter": {"max_cost_atr": .5},
                        "fixed_ticks": {"aa": {"stop_loss_ticks": 5, "take_profit_ticks": 10}},
                        "protection_scale": {"atr_multiple": 1., "roundtrip_cost_multiple": 2.}},
           "risk": {"initial_capital": 1000000, "trade_risk_fraction": .002, "portfolio_risk_fraction": .01,
                    "margin_fraction": .3, "group_fractions": {"commodity": .5}, "max_positions": 5,
                    "max_lots_per_contract": 3, "cost_buffer_multiple": 1.}}
    study = LabelStudy.__new__(LabelStudy)
    study.data = SimpleNamespace(cfg=cfg, by_day={(DAY, KEY): {b.datetime: b for b in bars}},
                                 metadata=SimpleNamespace(get=lambda key, day: meta),
                                 calendar=SimpleNamespace(minutes=lambda day, meta: schedule))
    study.schedule = {}
    study.plan = LABEL_PLAN
    study.parameters = SimpleNamespace(resolve=lambda key, time: (meta, []))
    study.allocator = RiskAllocator(cfg)
    record = row("signal", contract=KEY, direction=direction, atr=10., cost_distance=2.5, cost_pass=True,
                 signal_tick_size=1., signal_value_per_price=10.,
                 snapshot={"ma20": 100., "atr_previous": 10.}, execution_rejections=[],
                 quantity_signal_empty=3, protection={"stop_loss_ticks": 10})
    return study, record


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_next_open_signed_net_includes_both_fees_and_adverse_slippage(direction):
    study, record = label_study(direction)
    label = study.labels(record)["5"]
    assert label["raw_entry"] == 100 and label["raw_atr"] == pytest.approx(.4)
    assert label["net_per_lot_cny"] == pytest.approx(15)
    assert label["extra_tick_net_atr"] == pytest.approx(-.05)
    assert label["roundtrip_fee_cny"] == 5
    assert label["quantity_empty"] == 3


def test_scheduled_break_does_not_count_as_trading_minutes():
    schedule = [at(DAY, "09:15"), at(DAY, "09:16"), at(DAY, "10:00"), at(DAY, "10:01"), at(DAY, "10:02")]
    study, record = label_study(schedule=schedule)
    labels = study.labels(record)
    assert labels["5"]["exit_time"] == at(DAY, "10:03").isoformat()
    assert labels["15"]["reason"] == "day_ends_before_horizon"


def test_missing_scheduled_minute_is_censored_without_skipping_it():
    study, record = label_study()
    del study.data.by_day[(DAY, KEY)][at(DAY, "09:17")]
    assert study.labels(record)["5"]["reason"] == "scheduled_minute_missing"


def test_missing_execution_keeps_raw_path_and_never_substitutes_zero_cost():
    study, record = label_study()
    study.parameters.resolve = lambda key, time: (None, ["fee_unknown"])
    label = study.labels(record)["5"]
    assert label["raw_status"] == "complete" and label["economic_status"] == "unknown"
    assert "net_atr" not in label and "fee_unknown" in label["execution_rejections"]


def test_price_advantage_can_exist_when_original_account_cannot_afford_signal():
    study, record = label_study()
    record["quantity_signal_empty"] = 0
    label = study.labels(record)["5"]
    assert label["net_per_lot_cny"] > 0 and label["quantity_empty"] == 0
    assert not label["quantity_and_guard_feasible"]


def test_fill_cost_is_rechecked_and_cannot_override_the_original_gate():
    study, record = label_study()
    meta = study.data.metadata.get(KEY, DAY)
    meta["fees"][0]["open"]["value"] = 100
    label = study.labels(record)["5"]
    assert record["cost_pass"] is True and not label["cost_pass_at_open"]
    assert not label["quantity_and_guard_feasible"] and label["isolated_quantity_net_cny"] is None


def test_changed_contract_units_leave_economic_path_unknown():
    study, record = label_study()
    record["signal_tick_size"] = .5
    label = study.labels(record)["5"]
    assert label["raw_status"] == "complete" and label["reason"] == "contract_units_changed"
    assert "net_atr" not in label


def test_ma20_recovery_below_ma10_and_later_reclaim_are_one_touch():
    tracker = PullbackLabels("LONG", .1, 1, .2)
    previous = {"end": at(DAY, "09:11"), "close": 100., "ma10": 99., "ma20": 98., "high": 100.1, "low": 99.9}
    one = {"ma10": 99., "ma20": 98., "previous_atr": 1.}
    touch = bar("09:11", 98.2, low=98., high=98.6)
    assert not tracker.step(touch, one, previous, True, True, "session", True)
    pending = tracker.pending["touch_id"]
    previous = one | {"end": touch.end, "close": touch.close, "high": touch.high, "low": touch.low}
    recover = bar("09:12", 98.8, high=99.)
    first = tracker.step(recover, one, previous, True, True, "session", True)
    assert len(first) == 1 and first[0]["action"] == "recovered" and not first[0]["reclaimed_ma10"]
    previous = one | {"end": recover.end, "close": recover.close, "high": recover.high, "low": recover.low}
    second = tracker.step(bar("09:13", 99.5), one, previous, True, True, "session", True)
    assert len(second) == 1 and second[0]["action"] == "reclaimed_ma10"
    assert first[0]["touch_id"] == second[0]["touch_id"] == pending
    assert tracker.pending is None


@pytest.mark.parametrize("failure", ["higher", "gap", "extreme", "expiry"])
def test_waiting_pullback_expires_or_invalidates_before_confirmation(failure):
    tracker = PullbackLabels("LONG", .1, 1, .2)
    previous = {"end": at(DAY, "09:11"), "close": 100., "ma10": 99., "ma20": 98., "high": 100.1, "low": 99.9}
    one = {"ma10": 99., "ma20": 98., "previous_atr": 1.}
    touch = bar("09:11", 98.2, low=98., high=98.6)
    tracker.step(touch, one, previous, True, True, "session", True)
    previous = one | {"end": touch.end, "close": touch.close, "high": touch.high, "low": touch.low}
    probe = bar("09:21" if failure == "expiry" else "09:12", 99.5, low=97.5 if failure == "extreme" else 99.4)
    if failure == "expiry":
        previous["end"] = probe.datetime
    assert not tracker.step(probe, one, previous, failure != "higher", failure != "gap", "session", True)
    assert any(e["action"] == "cancelled" for e in tracker.events)
    assert not any(e["action"] == "recovered" for e in tracker.events)


def test_daily_aggregation_does_not_count_repeated_minutes_as_independent():
    rows = [row("repeat", contract="a", labels={"15": {"net_atr": 10.}}) for _ in range(100)]
    rows += [row("other", contract="b", labels={"15": {"net_atr": 0.}}),
             row("next", date="2026-01-09", contract="a", labels={"15": {"net_atr": 0.}})]
    result = estimate(daily_values(rows, "net_atr", 15).values())
    assert result["days"] == 2 and result["mean"] == 2.5


def test_future_price_changes_do_not_change_earlier_cohorts(tmp_path):
    original = load_data(create_fixture(tmp_path / "SYNTHETIC_LABELS_ONLY"))
    cfg = copy.deepcopy(original.cfg)
    cfg["strategy"].update(protection_scale={"atr_multiple": 1., "roundtrip_cost_multiple": 2.}, entry_cost_filter={"max_cost_atr": .5})
    data = Dataset(original.bars, cfg, daily=original.daily)
    future = at(DAY, "09:50")
    changed_bars = [replace(b, open=b.open+100, high=b.high+100, low=b.low+100, close=b.close+100)
                    if b.key == KEY and b.trading_day == DAY and b.datetime >= future else b for b in data.bars]
    changed = Dataset(changed_bars, cfg, daily=original.daily)
    candidate = {"contract": KEY, "product": "aa", "direction": "LONG", "group": "commodity", "rank": 1,
                 "selected": True, "r8": .01, "previous_volume": 1000}
    before = LabelStudy(data, Features(data), LABEL_PLAN).scan_candidate(DAY, candidate)[0]
    after = LabelStudy(changed, Features(changed), LABEL_PLAN).scan_candidate(DAY, candidate)[0]
    def earlier(rows):
        return [r for r in rows if r["time"] <= future.isoformat()]

    assert earlier(before) and earlier(before) == earlier(after)
