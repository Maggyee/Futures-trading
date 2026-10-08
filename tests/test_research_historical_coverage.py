import copy
import json

import pytest

from research.acquisition import DownloadAllowance
from research.data import file_sha256
from research.execution_parameters import validate_archive_reference
from research.config import ResearchError


@pytest.fixture
def frozen_archive(tmp_path):
    calibration = {"train_data_hash": "original_training_semantic_hash"}
    parent = {"strategy": {"k": 2, "efficiency_min": 0.45, "fixed_ticks": {"lc": [17,33]}},
              "risk": {"initial_capital": 1000000}, "calibration_snapshot": calibration,
              "splits": {"train": {"start": "2026-09-01", "end": "2026-09-11"},
                         "validation": {"start": "2026-09-14", "end": "2026-09-23"},
                         "test": {"start": "2026-09-24", "end": "2026-09-30"}}}
    parent_path = tmp_path / "parent.json"; parent_path.write_text(json.dumps(parent))
    source = tmp_path / "training_source"; source.mkdir()
    (source / "config_snapshot.json").write_text(json.dumps(parent))
    data = source / "data.jsonl.gz"; data.write_bytes(b"synthetic original immutable data")
    (source / "data_reference.json").write_text(json.dumps({"object": data.name, "sha256": file_sha256(data)}))
    plan = tmp_path / "plan.json"; plan.write_text(json.dumps({"months": ["2026-07", "2026-08"], "candidate_k": [2,5]}))
    archive = {"sample_status": "retrospective_after_calibration", "locked_test_read": False,
               "parent_config": str(parent_path), "parent_config_sha256": file_sha256(parent_path),
               "plan": str(plan), "plan_sha256": file_sha256(plan), "candidate_k": [2,5],
               "window": {"start": "2026-07-01", "end": "2026-07-31"},
               "training_reference_run": str(source), "training_reference_sha256": file_sha256(data)}
    cfg = copy.deepcopy(parent); cfg["archive_evaluation"] = archive
    q = {"archive_evaluation": archive, "validation_window": archive["window"]}
    return cfg, q, data


def test_retrospective_freeze_allows_declared_k_and_requires_intact_training_reference(frozen_archive):
    cfg,q,data = frozen_archive
    assert validate_archive_reference(cfg,q) == q["validation_window"]
    cfg["strategy"]["k"] = 5
    assert validate_archive_reference(cfg,q) == q["validation_window"]
    data.write_bytes(b"changed")
    with pytest.raises(ResearchError, match="原训练数据"):
        validate_archive_reference(cfg,q)


@pytest.mark.parametrize("drift", ["efficiency", "risk", "calibration", "locked_test", "other_k", "claim_oos", "split"])
def test_retrospective_declaration_rejects_retuning_or_test_access(frozen_archive, drift):
    cfg,q,_ = frozen_archive
    if drift == "efficiency": cfg["strategy"]["efficiency_min"] = 0.35
    elif drift == "risk": cfg["risk"]["initial_capital"] = 2000000
    elif drift == "calibration": cfg["calibration_snapshot"]["train_data_hash"] = "new fit"
    elif drift == "locked_test": cfg["archive_evaluation"]["window"].update(start="2026-09-24", end="2026-09-30")
    elif drift == "other_k": cfg["strategy"]["k"] = 10
    elif drift == "claim_oos": cfg["archive_evaluation"]["sample_status"] = "independent out of sample"
    elif drift == "split": cfg["splits"]["test"]["start"] = "2026-10-01"
    with pytest.raises(ResearchError): validate_archive_reference(cfg,q)


def test_download_allowance_shared_daily_limits_are_persistent(tmp_path):
    policy = {"ledger": str(tmp_path / "ledger.json"), "max_requests_per_day": 2, "max_bytes_per_day": 10}
    first,second = DownloadAllowance(policy),DownloadAllowance(policy)
    first.consume(6,requests=1)
    second.consume(4,requests=1)
    with pytest.raises(ResearchError, match="每日预算"): first.consume(requests=1)
    with pytest.raises(ResearchError, match="每日预算"): second.consume(1)
