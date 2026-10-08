"""Causal boundaries for replacement, trend windows and immediate protection."""

import copy
import csv
import gzip
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import load_data
from research.execution import Position
from research.fixtures import create_fixture
from research.opportunity_followup import BaselineEntries
from research.opportunity_rules import OpportunityBacktest, TrendWindowLogic
from research.reporting import funnel
from research.signal_journal import CompressedSignalJournal, SignalJournal

DAY, KEY = "2026-01-08", "aa2603.SHFE"
REPLACEMENT = {"policy": "skip_known_zero_capacity", "preserve_original_rank": True,
               "refresh": "completed_minute", "preserve_held_and_pending_slots": True}


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_OPPORTUNITY_TEST_ONLY")))


def engine(market, replacement=True):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["k"] = 2
    cfg.pop("baseline_expectation", None)
    if replacement:
        cfg["strategy"]["candidate_replacement"] = REPLACEMENT.copy()
    return OpportunityBacktest(market, cfg)


def candidates():
    return {f"c{rank}": {"contract": f"c{rank}", "rank": rank, "selected": rank <= 2,
                        "group": "commodity", "direction": "LONG"} for rank in range(1, 5)}


def test_zero_capacity_promotes_rank_three_without_reranking_or_mutating_archive(market):
    e, records = engine(market), candidates()
    archived = list(records.values())
    e.assess_capacity = lambda c, b, t: {"assessed": True, "quantity": 0 if c["rank"] == 1 else 1}
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert {k for k,c in records.items() if c["selected"]} == {"c2", "c3"}
    assert [c["rank"] for c in records.values()] == [1, 2, 3, 4]
    assert [c["selected"] for c in archived] == [True, True, False, False]
    assert len(e.candidate_selection) == 1


def test_unknown_capacity_keeps_original_slot_and_market_filters_do_not_drive_selection(market):
    e, records = engine(market), candidates()
    e.assess_capacity = lambda c,b,t: {"assessed": False, "reason": "execution_inputs_unavailable"}
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert {k for k,c in records.items() if c["selected"]} == {"c1", "c2"}
    assert not e.candidate_selection


def test_reserved_lower_rank_keeps_its_slot_when_higher_ranks_become_affordable(market):
    e, records = engine(market), candidates()
    e.state("c3").name = "ENTRY_PENDING"
    e.state("c3").pending = {"reserved_risk": 100, "reserved_margin": 100,
                              "group": "commodity"}
    e.assess_capacity = lambda c,b,t: {"assessed": True, "quantity": 1}
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert {k for k,c in records.items() if c["selected"]} == {"c1", "c3"}


def test_disabled_replacement_preserves_objects_and_selection(market):
    e, records = engine(market, False), candidates()
    before = copy.deepcopy(records)
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert records == before and not e.candidate_selection


def test_capacity_will_not_read_an_uncompleted_quote(market):
    e = engine(market)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:15")]
    assert e.assess_capacity({"contract": KEY}, bar, bar.datetime)["assessed"] is False


class Original:
    def __init__(self, data):
        self.data = data
        self.features = self

    def past(self, *unused):
        return None


def window(market, monkeypatch, source="09:15", touch="09:15"):
    original = Original(market)
    removed = ("efficiency", "trend_activity", "trend_displacement",
               "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum")
    def baseline(*args):
        return {"filters": {k: True for k in removed} | {"state": args[2], "entry_time": args[3]},
                "snapshot": {}, "exit_flags": []}
    w = TrendWindowLogic(original, baseline)
    w.higher_quality = lambda *unused: (True, {"source_5m_end": at(DAY, source).isoformat()})
    monkeypatch.setattr("research.opportunity_rules.pullback_event", lambda *unused: {
        "event": at(DAY, touch).isoformat(), "references": [10], "dual_touch": False, "epsilon": 1,
    })
    return w


@pytest.mark.parametrize("touch,expected", [("09:14", False), ("09:15", True)])
def test_only_completed_touch_at_or_after_arming_can_trigger(market, monkeypatch, touch, expected):
    w = window(market, monkeypatch, touch=touch)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:15")]
    result = w.evaluate(bar, {"direction": "LONG"}, True, True)
    assert result["filters"]["pullback_after_armed"] is expected
    assert result["all_pass"] is expected
    assert "efficiency" not in result["filters"]


def test_qualification_expires_without_new_completed_five_minute_bar(market, monkeypatch):
    w = window(market, monkeypatch)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:19")]
    result = w.evaluate(bar, {"direction": "LONG"}, True, True)
    assert not result["filters"]["trend_window_valid"]


def test_higher_trend_failure_cancels_the_observation(market, monkeypatch):
    w = window(market, monkeypatch)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:15")]
    assert w.evaluate(bar, {"direction": "LONG"}, True, True)["all_pass"]
    w.higher_quality = lambda *unused: (False, {"source_5m_end": at(DAY,"09:15").isoformat()})
    assert not w.evaluate(bar, {"direction": "LONG"}, True, True)["all_pass"]
    assert not w.windows


def test_pre_break_qualification_does_not_carry_to_next_session(market, monkeypatch):
    w = window(market, monkeypatch, source="10:00", touch="10:00")
    bar = market.by_day[(DAY, KEY)][at(DAY, "13:30")]
    assert not w.evaluate(bar, {"direction": "LONG"}, True, True)["filters"]["trend_window_valid"]


def seed(e):
    meta = e.data.metadata.get(KEY, DAY)
    p = Position(KEY, meta, 1, 1, 100, 100, at(DAY,"09:15"), DAY, 98, 104,
                 1, 100, 100, {"rank": 1,"r8": .01,"time": at(DAY,"09:15").isoformat(),"snapshot": {},"pullback": None})
    e.state(KEY).name, e.state(KEY).position = "LONG", p
    return p


def test_ma40_requires_two_adjacent_completed_bars_and_resets_on_gap(market):
    e = engine(market, False)
    e.cfg["strategy"].update(ma40_exit_confirmation_bars=2, enable_volume_exit=False)
    seed(e)
    one = pd.Series({"close": 99, "ma40": 100, "vr": 1, "previous_atr": 1})
    first = e.data.by_day[(DAY,KEY)][at(DAY,"09:15")]
    second = e.data.by_day[(DAY,KEY)][at(DAY,"09:16")]
    gap = e.data.by_day[(DAY,KEY)][at(DAY,"09:18")]
    assert e.position_exit_flags(KEY,first,one,None,"LONG") == []
    assert e.position_exit_flags(KEY,second,one,None,"LONG") == ["ma40_cross"]
    assert e.position_exit_flags(KEY,gap,one,None,"LONG") == []


def test_hard_stop_is_immediate_with_two_bar_trend_exit_confirmation(market):
    from research.execution import protective_touch
    e = engine(market, False)
    e.cfg["strategy"]["ma40_exit_confirmation_bars"] = 2
    p = seed(e)
    bar = replace(e.data.by_day[(DAY,KEY)][at(DAY,"09:15")], open=100,high=101,low=97,close=99)
    hit = protective_touch(p,bar)
    assert hit["reason"] == "fixed_stop" and hit["raw_price"] == 98


def test_compressed_journal_preserves_order_live_updates_and_repeated_exports(tmp_path):
    a = SignalJournal(tmp_path/"plain.partial",None)
    b = CompressedSignalJournal(tmp_path/"compressed.partial",None)
    live = {"trigger": True,"filled": False,"value": 2}
    for j in (a,b):
        j.append({"trigger": False,"value": 1})
        j.append(live)
        j.append({"trigger": False,"value": 3})
    live["filled"] = True
    assert list(a) == list(b) == list(b)
    live["value"] = 4
    assert list(b)[1]["value"] == 4
    with pytest.raises(ValueError):
        b.append({"trigger": False})
    a.discard()
    b.discard()


def test_invalid_confirmation_and_candidate_policy_are_rejected(market):
    e = engine(market)
    e.cfg["strategy"]["ma40_exit_confirmation_bars"] = True
    with pytest.raises(ResearchError):
        validate_config(e.cfg)
    e.cfg["strategy"]["ma40_exit_confirmation_bars"] = 2
    e.cfg["strategy"]["candidate_replacement"]["refresh"] = "future_close"
    with pytest.raises(ResearchError):
        validate_config(e.cfg)


def test_cached_measurements_restore_source_rejection_order_and_current_state():
    source = {"candidate": True, "trend_15m": False, "oi": False,
              "extension": False, "state": True, "entry_time": True}
    entry = BaselineEntries.__new__(BaselineEntries)
    entry.checked, entry.reused, entry.fresh, entry.skipped = set(), 0, 0, 0
    entry.filter_order = None
    moment = at(DAY, "09:16")
    row = {"time": moment.isoformat(), "contract": KEY, "direction": "LONG", "rank": "1",
           "filters": json.dumps(source, sort_keys=True), "snapshot": "{}",
           "exit_flags": "[]", "pullback": ""}
    entry.peek, entry.reader = row, iter([])
    def evaluate(bar, candidate, allows, cutoff):
        return {"filters": source | {"state": allows}, "snapshot": {}, "exit_flags": []}
    entry.original = SimpleNamespace(evaluate=evaluate)
    result = entry.baseline(SimpleNamespace(end=moment, key=KEY),
                            {"direction": "LONG", "rank": 1, "selected": True}, False, True)
    assert tuple(result["filters"]) == tuple(source)
    assert result["rejections"] == ["trend_15m", "oi", "extension", "state"]


@pytest.mark.parametrize("change", ["efficiency", "slope", "quality", "unknown_rule"])
def test_changed_rules_recompute_every_observation_instead_of_old_booleans(market, tmp_path, change):
    from pathlib import Path

    original = copy.deepcopy(market.cfg)
    (tmp_path/"config_snapshot.json").write_text(json.dumps(original))
    source = Path(__file__).resolve().parents[1]/"research"
    with tarfile.open(tmp_path/"source_snapshot.tar.gz","w:gz") as archive:
        for name in ("signals.py","refinements.py"):
            archive.add(source/name,arcname="research/"+name)
    with gzip.open(tmp_path/"signals.csv.gz","wt") as stream:
        writer = csv.DictWriter(stream,fieldnames=["filters"])
        writer.writeheader()
        writer.writerow({"filters":json.dumps({"efficiency":True})})
    cfg = copy.deepcopy(original)
    if change == "efficiency":
        cfg["strategy"]["efficiency_min"] = .99
    elif change == "slope":
        cfg["strategy"]["slope_band"] = {"changed":True}
    elif change == "quality":
        cfg["strategy"]["trend_quality"] = {"changed":True}
    else:
        cfg["strategy"]["future_new_gate"] = {"threshold":.9}
    entry = BaselineEntries(tmp_path,cfg)
    assert not entry.reuse_filters
    calls = []
    def evaluate(*args):
        calls.append(args)
        return {"filters":{"efficiency":False},"all_pass":False}
    entry.bind(SimpleNamespace(evaluate=evaluate,data=market,features=SimpleNamespace()))
    with entry:
        for clock in ("09:16","09:17","09:18"):
            row = entry.baseline(SimpleNamespace(end=at(DAY,clock),key=KEY),{})
            assert not row["all_pass"]
        entry.finish()
    assert len(calls) == 3 and entry.evidence["all_filter_decisions_recomputed"]


def test_trend_funnel_uses_actual_higher_quality_and_consumable_pullback_gates():
    flags = {name: True for name in ("candidate", "warmup_1m", "warmup_higher", "current_session_15m",
              "trend_15m", "trend_5m", "trend_1m", "vwap", "oi", "shock", "extension",
              "higher_trend_quality", "trend_window_valid", "pullback_after_armed")}
    row = {"filters": flags, "trigger": False, "risk_pass": False, "filled": False,
           "rejections": [], "risk_rejections": []}
    assert funnel([row])["sequential"]["smooth"] == 1
    flags["pullback_after_armed"] = False
    row["rejections"] = ["pullback_after_armed"]
    assert funnel([row])["sequential"]["smooth"] == 0
