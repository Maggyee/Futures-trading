"""Verify monthly archive references and the public automatic data reader."""

import copy
import json
from pathlib import Path

from .cloud_archive import CloudArchive
from .config import ResearchError
from .coverage_expansion import DEFAULT_PLAN, declaration
from .data import file_sha256, load_data
from .reporting import write_json
from .storage import SpaceBudget


def build(plan_path=DEFAULT_PLAN):
    path, plan, _ = declaration(plan_path)
    root = Path(plan["budget"]["roots"][1])
    archive = CloudArchive(plan["archive_policy"])
    budget = SpaceBudget(plan["budget"])
    configs = []
    with archive.locked():
        status = json.loads((archive.state / "status.json").read_text())
        if status["status"] != "verified":
            raise ResearchError("等待云端快照校验通过")
        manifest_id, manifest = archive._manifest(status["manifest_id"])
        files = {row["logical_path"]: row for row in manifest["files"]}
        for month in plan["months"]:
            cfg = json.loads((root / month / "historical_config.json").read_text())
            count = 0
            for group in ["sources", "daily_sources"]:
                for source in cfg["data"].get(group, []):
                    local = Path(source["path"]).resolve()
                    if not local.is_relative_to(path.parent):
                        raise ResearchError("月度行情超出本轮归档输入目录")
                    logical = "expanded_inputs/" + str(local.relative_to(path.parent))
                    if logical not in files or files[logical]["sha256"] != file_sha256(local):
                        raise ResearchError("云端快照尚未包含完整且一致的月度数据：" + logical)
                    source["cloud"] = {"policy": plan["archive_policy"], "manifest_id": manifest_id,
                                       "logical_path": logical}
                    count += 1
            write_json(root / month / "cloud_read_config.json", cfg, budget)
            configs.append({"month": month, "sources": count, "manifest_id": manifest_id})
    # Exercise load_data itself; cloud references are resolved automatically even
    # while the original local files are present. This is a one-contract reader
    # acceptance projection, not an additional strategy backtest.
    cfg = json.loads((root / "2026-08/cloud_read_config.json").read_text())
    source = cfg["data"]["sources"][0]
    contract = Path(source["path"]).stem
    cfg["data"]["sources"] = [source]
    cfg["data"]["expected_contract_days"] = [row for row in cfg["data"]["expected_contract_days"]
                                             if row["contract"] == contract]
    local_cfg = copy.deepcopy(cfg)
    for group in ["sources", "daily_sources"]:
        for row in local_cfg["data"].get(group, []):
            row.pop("cloud", None)
    cutoff = cfg["archive_evaluation"]["window"]["end"]
    local_data = load_data(local_cfg, cutoff=cutoff)
    cloud_data = load_data(cfg, cutoff=cutoff)
    if local_data.fingerprint != cloud_data.fingerprint or local_data.bars != cloud_data.bars:
        raise ResearchError("公共读取流程的云端数据与本地数据不一致")
    if cloud_data.quality["errors"]:
        raise ResearchError("云端读取验收的数据质量检查未通过")
    proof = {"status": "passed", "manifest_id": manifest_id, "configs": configs,
             "logical_path": source["cloud"]["logical_path"], "sha256": file_sha256(source["path"]),
             "normalized_rows": len(cloud_data.bars), "daily_rows": len(cloud_data.daily),
             "matched_original": True, "source_reader": "research.data.load_data",
             "semantic_fingerprint": cloud_data.fingerprint,
             "local_files_present_but_cloud_reader_exercised": True,
             "cache_limit_bytes": archive.policy["cache_max_bytes"]}
    write_json(root / "cloud_read_verification.json", proof, budget)
    print(json.dumps(proof, ensure_ascii=False))
    return proof


if __name__ == "__main__":
    build()
