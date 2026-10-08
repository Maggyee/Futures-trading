"""Losslessly compress remaining completed test journals under the same budget."""

import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

from research.data import file_sha256
from research.storage import BoundedFile, SpaceBudget, write_bounded_json

here = Path(__file__).parent
plan = json.loads((here / "ordered_opportunity_plan.json").read_text())
budget = SpaceBudget(plan["budget"])
receipt = here / "ordered_optimization_storage_tail.json"
if receipt.exists():
    raise RuntimeError("已有压缩凭据，不覆盖")
before = budget.check(here)
records = []
paths = sorted(
    p for p in (here / "test_tmp").rglob("*")
    if p.is_file() and not p.is_symlink()
    and p.suffix in {".html", ".json", ".csv"} and p.stat().st_size >= 32768
)
for path in paths:
    if subprocess.run(["fuser", "-s", str(path)], check=False).returncode == 0:
        raise RuntimeError("测试文件仍在使用：" + str(path))
    stat = path.stat()
    checksum = file_sha256(path)
    target = path.with_suffix(path.suffix + ".gz")
    if target.exists():
        raise RuntimeError("压缩目标已存在：" + str(target))
    with target.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", mtime=0,
                           fileobj=BoundedFile(raw, budget, target), compresslevel=6) as zipped:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    zipped.write(chunk)
    recovered, length = hashlib.sha256(), 0
    with gzip.open(target, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            recovered.update(chunk)
            length += len(chunk)
    if recovered.hexdigest() != checksum or length != stat.st_size:
        raise RuntimeError("解压核对失败：" + str(path))
    if path.stat().st_mtime_ns != stat.st_mtime_ns or file_sha256(path) != checksum:
        raise RuntimeError("原测试文件在压缩期间改变")
    os.chmod(target, stat.st_mode & 0o777)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    records.append({"original": str(path), "compressed": str(target),
                    "original_sha256": checksum, "original_bytes": length,
                    "compressed_sha256": file_sha256(target),
                    "compressed_bytes": target.stat().st_size, "recovery_verified": True})
    path.unlink()
    write_bounded_json(receipt, {"status": "in_progress", "before": before,
                                 "records": records}, budget)
after = budget.check(here)
released = sum(r["original_bytes"] - r["compressed_bytes"] for r in records)
write_bounded_json(receipt, {"status": "completed", "before": before, "after": after,
                             "records": records, "released_bytes": released}, budget)
print(json.dumps({"files": len(records), "released_bytes": released,
                  "used_bytes": after["used_bytes"], "max_bytes": after["max_bytes"]}))
