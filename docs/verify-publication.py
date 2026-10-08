"""Verify the published file bytes without importing or running trading code."""

import hashlib
import json
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "docs/publication_manifest.json").read_text())
    failures = []
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file() or digest(path) != expected["sha256"]:
            failures.append(name)
    if failures:
        raise SystemExit("文件缺失或指纹改变：" + ", ".join(failures))
    result = root / "research_outputs/structure_followup_2026-10-07"
    assessment = json.loads((result / "assessment.json").read_text())
    proof = json.loads((result / "trade_review/delivery_verification.json").read_text())
    assert assessment["status"] == "completed"
    assert assessment["selected"] == "control"
    assert len(assessment["scenarios"]) == 9
    assert proof["status"] == "passed" and proof["trade_count"] == 21
    for name, expected in proof["files"].items():
        assert digest(result / "trade_review" / name) == expected["sha256"], name
    print(json.dumps({
        "status": "passed", "files": len(manifest["files"]),
        "audited_runs": 9, "version_trade_records": 21,
        "unfilled_confirmation_cases": proof["unfilled_cases_checked"],
        "selected": assessment["selected"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
