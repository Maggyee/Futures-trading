"""Reuse verified immutable archive bytes for completed runs of this round."""

import json
import os
from pathlib import Path

from .data import file_sha256
from .storage import SpaceBudget, write_bounded_json


def completed_immutable_paths(root):
    paths = []
    for summary in root.glob("*/*/run_*/summary.json"):
        if json.loads(summary.read_text())["status"] != "completed":
            continue
        paths.extend(path for path in summary.parent.iterdir()
                     if path.is_file() and (path.name in {"manifest.json", "config_snapshot.json", "data_quality.json", "source_snapshot.tar.gz", "result.json.gz"}
                                            or path.name.endswith(".csv.gz")))
    report = root / "trade_review"
    delivery = report / "delivery_verification.json"
    if delivery.exists():
        proof = json.loads(delivery.read_text())
        if proof["status"] != "passed":
            raise RuntimeError("报告交付尚未通过核验")
        for name, record in proof["files"].items():
            path = report / name
            if path.parent != report or file_sha256(path) != record["sha256"]:
                raise RuntimeError("报告不可变文件指纹不符：" + name)
            paths.append(path)
    else:
        html = report / "ordered_opportunity_review.html"
        verification = report / "verification.json"
        # The HTML writer publishes by atomic replacement, so rebuilding also
        # preserves archive bytes linked before browser validation finishes.
        if (html.is_file() and not html.with_name(html.name + ".partial").exists()
                and verification.is_file() and json.loads(verification.read_text())["status"] == "passed"):
            paths.append(html)
    return paths


def reuse_archived_bytes(plan):
    root = Path(plan["output"])
    objects = Path(plan["budget"]["roots"][0]) / "cloud_state/objects"
    receipt = root / "archive_deduplication.json"
    records = json.loads(receipt.read_text())["records"] if receipt.exists() else []
    for path in completed_immutable_paths(root):
        stat = path.stat()
        checksum = file_sha256(path)
        source = objects / checksum[:2] / checksum
        if not source.exists():
            continue
        archived = source.stat()
        if (stat.st_dev, stat.st_ino) == (archived.st_dev, archived.st_ino):
            continue
        # New private artifacts may adopt the archive's stricter mode;
        # never alter a file already shared with older research paths.
        if stat.st_nlink == 1 and stat.st_mode & 0o777 == 0o664 and archived.st_mode & 0o777 == 0o600:
            os.chmod(path, 0o600)
            stat = path.stat()
        if ((stat.st_size, stat.st_mode, stat.st_uid, stat.st_gid) !=
                (archived.st_size, archived.st_mode, archived.st_uid, archived.st_gid)):
            continue
        if file_sha256(source) != checksum:
            raise RuntimeError("归档指纹不符：" + str(source))
        temporary = path.with_suffix(path.suffix + ".link.partial")
        os.link(source, temporary)
        os.replace(temporary, path)
        if file_sha256(path) != checksum:
            raise RuntimeError("不可变归档去重校验失败")
        records.append({"path": str(path), "source": str(source), "sha256": checksum,
                        "released_bytes": stat.st_size, "mode": oct(stat.st_mode & 0o777)})
    write_bounded_json(receipt, {"records": records,
        "released_bytes": sum(r["released_bytes"] for r in records)}, SpaceBudget(plan["budget"]))


if __name__ == "__main__":
    from .ordered_opportunity import PLAN

    plan = json.loads(PLAN.read_text())
    reuse_archived_bytes(plan)
    print(SpaceBudget(plan["budget"]).check(Path(plan["output"])))
