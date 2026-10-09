"""Decision clocks, rejected waits, entry-only eligibility and common endpoints."""

import copy
import importlib.util
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from test_research_opportunity_quality import label_study

from research.calendar import MINUTE, at, stamp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("timing_study", ROOT / "docs/study-decision-timing.py")
study_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study_module)
verify_spec = importlib.util.spec_from_file_location("timing_verifier", ROOT / "docs/verify-decision-timing.py")
verifier = importlib.util.module_from_spec(verify_spec)
verify_spec.loader.exec_module(verifier)


def clock(name, minute, base=True, strict=False, contract="aa2603.SHFE", rank=1, period="morning"):
    return {"id": name, "date": "2026-01-08", "time": (at("2026-01-08", "09:00") + minute * MINUTE).isoformat(),
            "minute_index": minute, "contract": contract, "direction": "LONG", "period": period,
            "base_pass": base, "strict_pass": strict, "product": "aa", "group": "commodity",
            "rank": rank, "rank_group": "rank_1_2" if rank <= 2 else "rank_6_plus",
            "session_profile": "commodity", "previous_volume": 100, "relative_atr": .01}


def test_first_strict_need_not_have_a_continuation_candle():
    rows = [clock("base", 15), clock("strict", 17, base=False, strict=True), clock("later", 20, strict=True)]
    segments = study_module.make_segments(rows)
    assert len(segments) == 1 and segments[0]["strict_id"] == "strict"
    assert segments[0]["wait_trading_minutes"] == 2
    assert rows[1]["first_strict"] and not rows[2]["first_strict"]


def test_never_strict_is_retained_and_later_executable_does_not_reselect():
    rows = [clock("base", 15), clock("refused", 17, strict=True), clock("executable", 20, strict=True),
            clock("never", 60), clock("never_later", 65)]
    rows[1]["entry_outcome"] = "refused"
    rows[2]["entry_outcome"] = "executable"
    segments = study_module.make_segments(rows)
    assert len(segments) == 2
    assert segments[0]["strict_id"] == "refused"
    assert segments[1]["strict_id"] is None


@pytest.mark.parametrize("minute,in_segment", [(45, True), (46, False)])
def test_causal_wait_expiry_is_not_extended_by_a_later_recovery(minute, in_segment):
    rows = [clock("base", 15), clock("strict", minute, base=False, strict=True), clock("new_base", 50)]
    segments = study_module.make_segments(rows)
    assert (segments[0]["strict_id"] is not None) == in_segment
    assert rows[1]["segment_id"] == ("base" if in_segment else None)
    assert segments[-1]["base_id"] == "new_base"


def test_session_change_cannot_continue_a_wait():
    rows = [clock("base", 15), clock("after_break", 16, base=False, strict=True, period="afternoon")]
    segments = study_module.make_segments(rows)
    assert segments[0]["strict_id"] is None and rows[1]["segment_id"] is None


def test_future_labels_cannot_change_first_clocks_or_phase_matches():
    rows = [clock("a", 15), clock("b", 15, contract="bb2603.SHFE", rank=6)]
    study_module.make_segments(rows)
    rule = {"comparisons": [["rank_1_2", "rank_6_plus"]], "maximum_liquidity_ratio": 4, "maximum_relative_atr_ratio": 2}
    expected = study_module.phase_matches(rows, rule)
    assert len(expected) == 1
    changed = copy.deepcopy(rows)
    for r in changed:
        r.update(labels={"15": {"net_atr": 10000 if r["rank"] == 6 else -10000}}, future_execution_available=False)
    assert study_module.phase_matches(changed, rule) == expected


def test_phase_matching_excludes_mid_segment_control():
    rows = [clock("earlier_control", 14, contract="bb2603.SHFE", rank=6),
            clock("target", 15), clock("mid_control", 15, contract="bb2603.SHFE", rank=6)]
    study_module.make_segments(rows)
    rule = {"comparisons": [["rank_1_2", "rank_6_plus"]], "maximum_liquidity_ratio": 4, "maximum_relative_atr_ratio": 2}
    assert study_module.phase_matches(rows, rule) == []


@pytest.mark.parametrize("age_difference,expected", [(5, 1), (6, 0)])
def test_first_strict_match_requires_close_segment_age(age_difference, expected):
    rows = [clock("base_a", 15), clock("strict_a", 25, strict=True),
            clock("base_b", 15 + age_difference, contract="bb2603.SHFE", rank=6),
            clock("strict_b", 25, strict=True, contract="bb2603.SHFE", rank=6)]
    study_module.make_segments(rows)
    rule = {"comparisons": [["rank_1_2", "rank_6_plus"]], "maximum_liquidity_ratio": 4, "maximum_relative_atr_ratio": 2}
    pairs = [p for p in study_module.phase_matches(rows, rule) if p["stage"] == "first_strict"]
    assert len(pairs) == expected


def timing_fixture(direction="LONG", delay=2, strict_atr=10):
    study, base = label_study(direction)
    base["time"] = at("2026-01-08", "09:15").isoformat()
    base["metadata_tick"] = 1
    strict = copy.deepcopy(base)
    strict.update(time=(at("2026-01-08", "09:15") + delay * MINUTE).isoformat(), atr=strict_atr)
    base["labels"] = study_module.labels(study, base, (5, 15, 30))
    strict["labels"] = study_module.labels(study, strict, [5, 15, 30] + [h-delay for h in (5, 15, 30) if h > delay])
    return study, base, strict


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_common_endpoint_separates_entry_change_from_own_horizon(direction):
    _, base, strict = timing_fixture(direction)
    common = study_module.paired_labels(base, strict, 2, True)["15"]
    own = study_module.paired_labels(base, strict, 2, False)["15"]
    assert common["base_exit_time"] == common["strict_exit_time"]
    assert common["raw_delta_atr_base"] == pytest.approx(-.2)
    assert own["base_exit_time"] != own["strict_exit_time"]
    assert own["raw_delta_atr_base"] == pytest.approx(0)


def test_changed_strict_atr_cannot_manufacture_a_paired_improvement():
    _, base, strict = timing_fixture(strict_atr=50)
    paired = study_module.paired_labels(base, strict, 2, True)["15"]
    assert paired["raw_delta_atr_base"] == pytest.approx(-.2)
    assert paired["raw_delta_ticks"] == pytest.approx(-2)
    assert paired["raw_delta_bps_base"] == pytest.approx(-200)


@pytest.mark.parametrize("delay", [15, 16])
def test_late_strict_kept_but_cannot_use_base_common_endpoint(delay):
    _, base, strict = timing_fixture(delay=delay)
    common = study_module.paired_labels(base, strict, delay, True)["15"]
    assert common["raw_status"] == "censored"
    assert common["reason"] == "late_strict_no_remaining_minutes"
    assert "net_delta_atr_base" not in common
    assert study_module.paired_labels(base, strict, delay, False)["5"]["raw_status"] == "complete"


def test_future_exit_metadata_does_not_determine_entry_eligibility():
    study, row = label_study()
    row["time"] = at("2026-01-08", "09:15").isoformat()
    meta, _ = study.parameters.resolve(row["contract"], row["time"])
    study.parameters.resolve = lambda key, time: (meta, []) if stamp(time) <= at("2026-01-08", "09:15") else (None, ["future_fee_unknown"])
    eligibility = study_module.execution_at_open(study, row)
    assert eligibility["status"] == "executable_empty_account"
    assert study_module.labels(study, row, [15])["15"]["economic_status"] == "unknown"


def test_known_cost_refusal_not_changed_by_positive_future_label():
    study, row = label_study()
    row["time"] = at("2026-01-08", "09:15").isoformat()
    row["cost_pass"] = False
    row["labels"] = {"15": {"net_atr": 10000}}
    result = study_module.execution_at_open(study, row)
    assert result["status"] == "not_executable" and "signal_cost" in result["rejections"]


def test_missing_next_open_is_unknown_and_not_skipped():
    study, row = label_study()
    row["time"] = at("2026-01-08", "09:15").isoformat()
    del study.data.by_day[(row["date"], row["contract"])][at("2026-01-08", "09:15")]
    result = study_module.execution_at_open(study, row)
    assert result["status"] == "unknown"
    assert result["entry_time"] == row["time"]
    assert "scheduled_entry_minute_missing" in result["unknown"]


def test_pair_without_complete_costs_has_no_net_difference():
    _, base, strict = timing_fixture()
    strict["labels"]["13"]["economic_status"] = "unknown"
    for k in list(strict["labels"]["13"]):
        if k.startswith("net_"):
            del strict["labels"]["13"][k]
    result = study_module.paired_labels(base, strict, 2, True)["15"]
    assert result["raw_status"] == "complete" and result["economic_status"] == "unknown"
    assert "net_delta_atr_base" not in result


def test_old_matching_audit_preserves_pairs_and_reports_mid_segment_controls():
    target, control, later = clock("target", 25), clock("control", 15, rank=6), clock("later", 25, rank=6)
    target.update(segment_id="target", representative=True)
    control.update(segment_id="control", representative=True)
    later.update(segment_id="control", representative=False)
    pair = {"treatment": "target", "control": "later", "distance": .2}
    checked = study_module.old_stage_audit([target, control, later], [pair])[0]
    assert checked["treatment_age"] == 0 and checked["control_age"] == 10
    assert checked["segment_age_difference"] == 10 and not checked["both_first"]
    assert all(checked[k] == v for k, v in pair.items())


def test_independent_verifier_detects_false_executable_price_guard():
    study, row = label_study()
    row["time"] = at("2026-01-08", "09:15").isoformat()
    schedule = study.minutes(row["date"], row["contract"])
    bars = study.data.by_day[(row["date"], row["contract"])]
    bars[schedule[0]] = replace(bars[schedule[0]], open=120, high=121, low=119, close=120)
    prices = {(row["date"], row["contract"], b.end.isoformat()): asdict(b) for b in bars.values()}
    row["entry_eligibility"] = study_module.execution_at_open(study, row)
    assert row["entry_eligibility"]["rejections"] == ["next_open_price_guard"]
    verifier.independently_check_entry(row, prices, schedule, study.parameters, study.data.cfg)
    row["entry_eligibility"]["price_guard_pass"] = True
    with pytest.raises(AssertionError):
        verifier.independently_check_entry(row, prices, schedule, study.parameters, study.data.cfg)


def test_independent_verifier_retains_unknown_exact_next_bar():
    study, row = label_study()
    row["time"] = at("2026-01-08", "09:15").isoformat()
    schedule = study.minutes(row["date"], row["contract"])
    del study.data.by_day[(row["date"], row["contract"])][schedule[0]]
    row["entry_eligibility"] = study_module.execution_at_open(study, row)
    verifier.independently_check_entry(row, {}, schedule, study.parameters, study.data.cfg)
    assert row["entry_eligibility"]["status"] == "unknown"
