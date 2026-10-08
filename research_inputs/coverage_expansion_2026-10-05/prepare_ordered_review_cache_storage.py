"""Losslessly archive rebuild caches; verify all published files are preserved."""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

from research.data import file_sha256
from research.storage import BoundedFile, SpaceBudget, write_bounded_json

here = Path(__file__).parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--latest", action="store_true")
args = parser.parse_args()
plan = json.loads((here / "ordered_opportunity_plan.json").read_text())
budget = SpaceBudget(plan["budget"])
receipt = here / ("ordered_latest_cache_storage.json" if args.latest else "ordered_review_cache_storage.json")
if receipt.exists():
    raise RuntimeError("已有缓存压缩凭据，不覆盖")
before = budget.check(here)
records = []
root = Path(plan["budget"]["roots"][1])
variants = ("latest_trade_review",) if args.latest else ("frequency_followup", "opportunity_followup")
for variant in variants:
    directory = root / variant if args.latest else root / variant / "trade_review"
    proof_path = directory / "delivery_verification.json"
    proof_sha = file_sha256(proof_path)
    proof = json.loads(proof_path.read_text())
    if proof["status"] != "passed":
        raise RuntimeError("历史交付尚未通过核验")
    if "files" in proof:
        published = proof["files"]
    elif args.latest:
        for field in ("html", "zip"):
            delivered = Path(proof[field])
            if delivered.parent != directory.resolve() or file_sha256(delivered) != proof[field + "_sha256"]:
                raise RuntimeError("历史交付指纹不符：" + field)
        published = {p.name: {"sha256": file_sha256(p)} for p in directory.iterdir()
                     if p.is_file() and p.name != "review_data.json"}
    else:
        raise RuntimeError("历史交付凭据缺少文件清单")
    for name, details in published.items():
        if file_sha256(directory / name) != details["sha256"]:
            raise RuntimeError("已交付文件校验失败：" + name)
    path = directory / "review_data.json"
    if subprocess.run(["fuser", "-s", str(path)], check=False).returncode == 0:
        raise RuntimeError("报告缓存仍在使用：" + str(path))
    stat = path.stat()
    checksum = file_sha256(path)
    target = path.with_suffix(".json.gz")
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
        raise RuntimeError("缓存解压核对失败：" + str(path))
    if path.stat().st_mtime_ns != stat.st_mtime_ns or file_sha256(path) != checksum:
        raise RuntimeError("原缓存在压缩期间改变")
    os.chmod(target, stat.st_mode & 0o777)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    for name, details in published.items():
        if file_sha256(directory / name) != details["sha256"]:
            raise RuntimeError("已交付文件被改变：" + name)
    if file_sha256(proof_path) != proof_sha:
        raise RuntimeError("交付凭据被改变")
    records.append({"original": str(path), "compressed": str(target),
                    "original_sha256": checksum, "original_bytes": length,
                    "compressed_sha256": file_sha256(target),
                    "compressed_bytes": target.stat().st_size, "recovery_verified": True,
                    "delivery_proof_sha256": proof_sha,
                    "published_files_preserved": list(published)})
    path.unlink()
    write_bounded_json(receipt, {"status": "in_progress", "before": before,
                                 "records": records}, budget)
after = budget.check(here)
released = sum(r["original_bytes"] - r["compressed_bytes"] for r in records)
write_bounded_json(receipt, {"status": "completed", "before": before, "after": after,
                             "records": records, "released_bytes": released}, budget)
print(json.dumps({"files": len(records), "released_bytes": released,
                  "used_bytes": after["used_bytes"], "max_bytes": after["max_bytes"]}))
