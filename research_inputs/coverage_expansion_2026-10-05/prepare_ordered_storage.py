"""Losslessly archive completed test artifacts within the unchanged disk budget."""

import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

from research.data import file_sha256
from research.storage import BoundedFile, SpaceBudget, write_bounded_json

HERE = Path(__file__).parent
plan = json.loads((HERE / "opportunity_followup_plan.json").read_text())
budget = SpaceBudget(plan["budget"])
receipt = HERE / "ordered_optimization_storage.json"
if receipt.exists():
    raise RuntimeError("已有压缩凭据，不覆盖")
before = budget.check(HERE)
records = []
paths = sorted(
    p for p in (HERE / "test_tmp").rglob("*")
    if p.is_file() and p.suffix in {".html", ".json"} and p.stat().st_size >= 512 * 1024
)
for path in paths:
    if subprocess.run(["fuser", "-s", str(path)], check=False).returncode == 0:
        raise RuntimeError("测试文件仍在使用：" + str(path))
    stat = path.stat()
    source_sha = file_sha256(path)
    target = path.with_suffix(path.suffix + ".gz")
    if target.exists():
        raise RuntimeError("压缩目标已存在：" + str(target))
    with target.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", mtime=0,
                           fileobj=BoundedFile(raw, budget, target), compresslevel=6) as zipped:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    zipped.write(chunk)
    recovered, count = hashlib.sha256(), 0
    with gzip.open(target, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            recovered.update(chunk)
            count += len(chunk)
    if recovered.hexdigest() != source_sha or count != stat.st_size:
        raise RuntimeError("解压核对失败：" + str(path))
    if path.stat().st_mtime_ns != stat.st_mtime_ns or file_sha256(path) != source_sha:
        raise RuntimeError("原测试文件在压缩期间改变")
    os.chmod(target, stat.st_mode & 0o777)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    records.append({"original": str(path), "compressed": str(target),
                    "original_sha256": source_sha, "original_bytes": count,
                    "compressed_sha256": file_sha256(target),
                    "compressed_bytes": target.stat().st_size, "recovery_verified": True})
    # Only the obsolete test representation is replaced; all bytes are recoverable.
    path.unlink()
    write_bounded_json(receipt, {"status": "in_progress", "before": before,
                                 "records": records}, budget)
after = budget.check(HERE)
write_bounded_json(receipt, {"status": "completed", "before": before, "after": after,
                             "records": records,
                             "released_bytes": before["used_bytes"] - after["used_bytes"]}, budget)
print(json.dumps({"files": len(records), "released_bytes": before["used_bytes"] - after["used_bytes"],
                  "used_bytes": after["used_bytes"], "max_bytes": after["max_bytes"]}))
