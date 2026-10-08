"""Package rendered chart artifacts only after source and browser verification."""

import csv
import gzip
import io
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from research.data import file_sha256
from research.frequency_followup import require
from research.ordered_opportunity import PLAN
from research.storage import BoundedFile, SpaceBudget, write_bounded_json

os.umask(0o077)
plan = json.loads(PLAN.read_text())
root = Path(plan["output"]) / "trade_review"
with gzip.open(root / "review_data.json.gz", "rt") as stream:
    data = json.load(stream)
proof = json.loads((root / "browser_verification.json").read_text())
html = root / "ordered_opportunity_review.html"
require(
    proof["status"] == "passed" and proof["html_sha256"] == file_sha256(html),
    "浏览器核验来源不符",
)
require(len(proof["checked_views"]) == 2 * len(data["charts"]), "图表核验不完整")
require(data["verification"]["status"] == "passed", "缺少数据审计")
budget = SpaceBudget(plan["budget"])
paths = [root / (c["trade"]["uid"] + ".png") for c in data["charts"]]
paths += [
    root / "trade_review.csv",
    root / "selected_trades_export.csv",
    root / "review_notes.md",
    root / "ordered_opportunity_review.html",
    root / "selection_diagnosis.json",
    root / "assessment.json",
    root / "verification.json",
    root / "browser_verification.json",
]
require(all(p.is_file() for p in paths), "缺少图片或清单")
require(file_sha256(root / "assessment.json") == file_sha256(Path(plan["output"]) / "assessment.json"),
        "报告中的筛选明细与回测评估不符")
archive_source_bytes = sum(p.stat().st_size for p in paths)
with (root / "trade_review.csv").open(encoding="utf-8-sig", newline="") as stream:
    records = list(csv.DictReader(stream))
require(len(records) == len(data["charts"]), "成交清单数量不符")
target = root / "进出场图与成交清单.zip"
# Measure the completed compressed archive in memory, then charge its exact
# size before publication and each actual disk write. Replace atomically so
# previous archives also remain intact when their bytes are shared.
temporary = target.with_name(target.name + ".partial")
require(not temporary.exists(), "已有未完成压缩包，保留并停止")
with io.BytesIO() as compressed:
    with ZipFile(compressed, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths:
            archive.write(path, path.name)
    archive_reserve = compressed.tell()
    budget.check(temporary, reserve=archive_reserve)
    compressed.seek(0)
    with temporary.open("xb") as raw:
        with io.BufferedWriter(BoundedFile(raw, budget, temporary), buffer_size=1024 * 1024) as sink:
            while chunk := compressed.read(1024 * 1024):
                sink.write(chunk)
    require(temporary.stat().st_size == archive_reserve, "压缩包实际写入体积不符")
    temporary.replace(target)
with ZipFile(target) as archive:
    require(archive.testzip() is None, "图表压缩包校验失败")
delivery = {
    "status": "passed",
    "charts": len(data["charts"]),
    "selected": data["selected"],
    "selected_trades": data["assessment"]["totals"][data["selected"]]["trade_count"],
    "files": {
        p.name: {"sha256": file_sha256(p), "bytes": p.stat().st_size}
        for p in paths + [target, html]
    },
    "source_checks": len(data["verification"]["checks"]),
    "browser_views": len(proof["checked_views"]),
    "locked_test_read": False,
    "live_trading_changed": False,
    "archive_reserve_bytes": archive_reserve,
    "archive_source_bytes": archive_source_bytes,
    "archive_size_measured_before_write": True,
}
write_bounded_json(root / "delivery_verification.json", delivery, budget)
budget.check(root)
print(
    json.dumps(
        {
            "status": "passed",
            "charts": delivery["charts"],
            "selected_trades": delivery["selected_trades"],
            "zip": str(target),
        },
        ensure_ascii=False,
    )
)
