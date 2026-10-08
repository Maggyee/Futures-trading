import copy
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from research.calendar import at
from research.config import ResearchError
from research.prepared_entries import PreparedEntries
from research.reporting import write_csv


@pytest.fixture
def prepared(tmp_path):
    cfg = {k: {} for k in ("risk", "metadata", "calendar", "execution", "calibration_snapshot")}
    cfg.update(strategy={"entry_mode": "direct"}, splits={"validation": {"start": "2026-01-08", "end": "2026-01-08"}})
    (tmp_path / "config_snapshot.json").write_text(json.dumps(cfg))
    (tmp_path / "manifest.json").write_text(json.dumps({"data_fingerprint": "verified", "window": cfg["splits"]["validation"], "split": "validation", "scope": "shared"}))
    root = Path(__file__).resolve().parents[1]
    with tarfile.open(tmp_path / "source_snapshot.tar.gz", "w:gz") as archive:
        for name in ("signals.py", "refinements.py"):
            archive.add(root / "research" / name, arcname="research/" + name)
    bar = SimpleNamespace(end=at("2026-01-08", "09:15"), trading_day="2026-01-08", key="aa2603.SHFE")
    candidate = {"direction": "LONG", "rank": 1}
    evaluation = {"filters": {"state": False, "entry_time": True, "trend": True}, "snapshot": {"ma20": 100.0}, "pullback": None, "exit_flags": []}
    write_csv(tmp_path / "signals.csv.gz", [{"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, **candidate, **evaluation, "filled": True, "net_pnl": 9999}])
    logic = SimpleNamespace(evaluate=lambda *args: copy.deepcopy(evaluation))
    return tmp_path, cfg, bar, candidate, logic


def test_replay_recomputes_state_and_rejects_unconsumed_or_extra_observations(prepared):
    run, cfg, bar, candidate, logic = prepared
    entries = PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"})
    with entries.bind(logic):
        with pytest.raises(ResearchError, match="未回放"):
            entries.finish()
    with entries.bind(logic):
        actual = entries.evaluate(bar, candidate, state_allows=True)
        assert actual["all_pass"] and actual["rejections"] == []
        assert list(actual["filters"]) == ["state", "entry_time", "trend"]
        assert "filled" not in actual and "net_pnl" not in actual
        entries.finish()
        with pytest.raises(ResearchError, match="提前结束"):
            entries.evaluate(bar, candidate)


@pytest.mark.parametrize("drift", ["data", "strategy", "identity", "first_calculation", "entry_time", "source"])
def test_snapshot_reuse_rejects_drift(prepared, drift):
    run, cfg, bar, candidate, logic = prepared
    evidence = {"retained_data_fingerprint": "verified"}
    if drift == "data":
        evidence["retained_data_fingerprint"] = "different"
    elif drift == "strategy":
        cfg["strategy"]["entry_mode"] = "pullback"
    elif drift == "identity":
        candidate["rank"] = 2
    elif drift == "first_calculation":
        original = logic.evaluate(None)
        original["snapshot"]["ma20"] = 101
        logic.evaluate = lambda *args: original
    elif drift == "source":
        altered = run / "altered.py"
        altered.write_text("# different entry logic\n")
        with tarfile.open(run / "source_snapshot.tar.gz", "w:gz") as archive:
            archive.add(altered, arcname="research/signals.py")
    with pytest.raises(ResearchError):
        with PreparedEntries(run, cfg, evidence).bind(logic) as entries:
            entries.evaluate(bar, candidate, before_cutoff=drift != "entry_time")


@pytest.mark.parametrize("value,state,expected", [(0.35, True, True), (0.349999, True, False), (0.44, False, False), (None, True, False), (-0.5, True, False)])
def test_efficiency_change_recomputes_only_declared_filter_and_state(prepared, value, state, expected):
    run, cfg, bar, candidate, logic = prepared
    cfg["strategy"].update(k=2, efficiency_min=0.45, enable_smooth_filter=True)
    cfg["strategy"]["trailing_exit"] = {"activation": "original_target", "atr_multiple": 2}
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    cfg["strategy"]["efficiency_min"] = 0.35
    evaluation = logic.evaluate(None)
    evaluation["filters"]["efficiency"] = False
    evaluation["snapshot"]["efficiency"] = value
    write_csv(run / "signals.csv.gz", [{"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, **candidate, **evaluation}])
    evaluation["filters"]["efficiency"] = value is not None and value >= 0.35
    logic.evaluate = lambda *args: copy.deepcopy(evaluation)
    with PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}, allow_efficiency_change=True).bind(logic) as entries:
        actual = entries.evaluate(bar, candidate, state_allows=state)
        assert actual["all_pass"] is expected
        assert actual["snapshot"]["efficiency"] == value
        entries.finish()
        assert entries.evidence["efficiency_filter_changes"] == int(value is not None and value >= 0.35)


@pytest.mark.parametrize("drift", ["k", "undeclared_efficiency", "invalid_efficiency", "disabled_smooth", "old_snapshot"])
def test_efficiency_reuse_rejects_other_drift_or_corrupt_source(prepared, drift):
    run, cfg, bar, candidate, logic = prepared
    cfg["strategy"].update(k=2, efficiency_min=0.45, enable_smooth_filter=True)
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    cfg["strategy"]["efficiency_min"] = 0.35
    if drift == "k":
        cfg["strategy"]["k"] = 5
    elif drift == "invalid_efficiency":
        cfg["strategy"]["efficiency_min"] = float("nan")
    elif drift == "disabled_smooth":
        cfg["strategy"]["enable_smooth_filter"] = False
    elif drift == "old_snapshot":
        evaluation = logic.evaluate(None)
        evaluation["filters"]["efficiency"] = True
        evaluation["snapshot"]["efficiency"] = 0.3
        write_csv(run / "signals.csv.gz", [{"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, **candidate, **evaluation}])
        logic.evaluate = lambda *args: copy.deepcopy(evaluation)
    with pytest.raises(ResearchError):
        with PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}, allow_efficiency_change=drift != "undeclared_efficiency").bind(logic) as entries:
            entries.evaluate(bar, candidate)


def test_unchanged_parent_with_trailing_can_be_replayed(prepared):
    run, cfg, bar, candidate, logic = prepared
    cfg["strategy"]["trailing_exit"] = {"activation": "original_target", "atr_multiple": 2}
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    with PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}).bind(logic) as entries:
        assert entries.evaluate(bar, candidate, state_allows=True)["all_pass"]
        entries.finish()


def test_snapshot_reuse_rejects_changed_observation_scope_before_replay(prepared):
    run, cfg, *_ = prepared
    cfg["storage"] = {"record_unselected_signals": False}
    with pytest.raises(ResearchError, match="记录范围"):
        PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"})


def test_selected_projection_skips_only_verified_unselected_rows_and_consumes_the_source(prepared):
    run, cfg, bar, candidate, logic = prepared
    cfg["strategy"]["k"] = 2
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    evaluation = logic.evaluate(None)
    evaluation["filters"]["candidate"] = True
    logic.evaluate = lambda *args: copy.deepcopy(evaluation)
    base = {"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, **candidate, **evaluation}
    other = {**base, "rank": 3, "filters": {**evaluation["filters"], "candidate": False}}
    write_csv(run / "signals.csv.gz", [other, base, other | {"rank": 4}])
    cfg["storage"] = {"record_unselected_signals": False}
    with PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}, allow_selected_projection=True).bind(logic) as entries:
        assert entries.evaluate(bar, candidate, state_allows=True)["all_pass"]
        entries.finish()
        assert entries.evidence["observations_replayed"] == 1
        assert entries.evidence["unselected_source_observations_projected_out"] == 2


@pytest.mark.parametrize("rank,expected", [(2, True), (3, True), (5, True), (6, False)])
def test_candidate_expansion_uses_original_rank_and_recomputes_state(prepared, rank, expected):
    run, cfg, bar, candidate, logic = prepared
    cfg["strategy"]["k"] = 2
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    cfg["strategy"]["k"] = 5
    candidate.update(rank=rank, selected=expected)
    evaluation = logic.evaluate(None)
    evaluation["filters"]["candidate"] = rank <= 2
    write_csv(run / "signals.csv.gz", [{"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, **candidate, **evaluation}])
    evaluation["filters"]["candidate"] = expected
    logic.evaluate = lambda *args: copy.deepcopy(evaluation)
    with PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}, allow_candidate_expansion=True).bind(logic) as entries:
        assert entries.evaluate(bar, candidate, state_allows=True)["all_pass"] == expected
        entries.finish()
        assert entries.evidence["candidate_filter_changes"] == int(3 <= rank <= 5)


def test_candidate_expansion_does_not_allow_quality_change(prepared):
    run, cfg, *_ = prepared
    cfg["strategy"].update(k=2, efficiency_min=0.45)
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    cfg["strategy"].update(k=5, efficiency_min=0.35)
    with pytest.raises(ResearchError):
        PreparedEntries(run, cfg, {"retained_data_fingerprint": "verified"}, allow_candidate_expansion=True)
