"""Snapshot the allowlisted tooling and original evidence needed for replay."""

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

from .config import ResearchError, digest
from .coverage_expansion import DEFAULT_PLAN, ORIGINAL, ROOT, declaration
from .data import file_sha256
from .reporting import write_json
from .storage import SpaceBudget


def build(plan_path=DEFAULT_PLAN):
    plan_path, plan, _ = declaration(plan_path)
    sources = {}
    for source in sorted((ROOT / "research").glob("*.py")):
        sources[str(source.relative_to(ROOT))] = source
    for source in sorted((ROOT / "tests").glob("test_research*.py")):
        sources[str(source.relative_to(ROOT))] = source
    for source in sorted((ROOT / "tests/fixtures").rglob("*")):
        if source.is_file() and not source.is_symlink():
            sources[str(source.relative_to(ROOT))] = source
    for relative in ["requirements.lock", "tests/conftest.py"]:
        source = ROOT / relative
        if source.is_file():
            sources[relative] = source
    for source in sorted((ROOT / "deploy/systemd").glob("quant-research-*")):
        sources[str(source.relative_to(ROOT))] = source
    source = ROOT / "deploy/systemd/rclone-googledrive-quant-config.conf"
    if source.exists():
        sources[str(source.relative_to(ROOT))] = source
    for name in ["audit_trailing_exit.py", "audit_slope_band.py"]:
        source = ROOT / "research_inputs/2026-09" / name
        sources[str(source.relative_to(ROOT))] = source
    # The parent contains only already opened development/training records.
    # Do not inspect September's locked test data while packaging evidence.
    for run in [Path(plan["parent_run"]), ORIGINAL]:
        cfg = json.loads((run / "config_snapshot.json").read_text())
        records = cfg.get("execution", {}).get("qualification", {}).get("sources", [])
        records += [record for rule in cfg.get("execution", {}).get("qualification", {}).get("rules", [])
                    for record in rule.get("sources", [])]
        for record in records:
            if "path" not in record:
                continue
            source = Path(record["path"]).resolve()
            if not source.is_relative_to(ROOT / "research_inputs/2026-09/source_docs"):
                raise ResearchError("复现证据超出已声明的官方资料目录")
            if file_sha256(source) != record["sha256"]:
                raise ResearchError("原执行证据校验不符")
            sources[str(source.relative_to(ROOT))] = source
    records = [{"relative": relative, "origin": str(source),
                "sha256": file_sha256(source), "bytes": source.stat().st_size}
               for relative, source in sorted(sources.items())]
    identity = digest(records)
    target = plan_path.parent / "reproducibility" / identity
    budget = SpaceBudget(plan["budget"])
    for record in records:
        source = sources[record["relative"]]
        destination = target / record["relative"]
        if destination.exists():
            if file_sha256(destination) != record["sha256"]:
                raise ResearchError("复现快照损坏，不覆盖")
            continue
        budget.check(destination, reserve=record["bytes"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".partial")
        temporary.write_bytes(source.read_bytes())
        if file_sha256(temporary) != record["sha256"]:
            raise ResearchError("生成复现快照时来源发生改变")
        temporary.replace(destination)
    manifest = {"schema": 1, "snapshot_id": identity, "files": records,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "credentials_included": False,
                "locked_test_read": False,
                "restore_note": "Preserves workspace-relative layout. Configuration paths must be remapped if restored under another root; cloud configs retain verified source references."}
    write_json(target / "manifest.json", manifest, budget)
    write_json(plan_path.parent / "reproducibility_latest.json",
               {"directory": str(target), "snapshot_id": identity, "files": len(records),
                "bytes": sum(record["bytes"] for record in records)}, budget)
    print(json.dumps({"snapshot_id": identity, "files": len(records),
                      "bytes": sum(record["bytes"] for record in records)}))
    return target


if __name__ == "__main__":
    build()
