"""Package rendered chart artifacts only after source and browser verification."""

import csv
import gzip
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from research.data import file_sha256
from research.opportunity_followup import PLAN, require
from research.storage import BoundedFile, SpaceBudget, write_bounded_json

plan = json.loads(PLAN.read_text())
root = Path(plan["output"]) / "trade_review"
cache = root / "review_data.json"
if cache.exists():
    data = json.loads(cache.read_text())
else:
    with gzip.open(cache.with_suffix(".json.gz"), "rt", encoding="utf-8") as stream:
        data = json.load(stream)
proof = json.loads((root / "browser_verification.json").read_text())
html = root / "opportunity_followup_review.html"
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
    root / "opportunity_followup_review.html",
    root / "selection_diagnosis.json",
    root / "verification.json",
    root / "browser_verification.json",
]
require(all(p.is_file() for p in paths), "缺少图片或清单")
# Reserve a conservative bound for the actual archive, including ZIP headers
# and small deflate expansion. Charge each output chunk as well.
archive_source_bytes = sum(p.stat().st_size for p in paths)
archive_reserve = archive_source_bytes + max(1024 * 1024, archive_source_bytes // 100)
budget.check(root, reserve=archive_reserve)
with (root / "trade_review.csv").open(encoding="utf-8-sig", newline="") as stream:
    records = list(csv.DictReader(stream))
require(len(records) == len(data["charts"]), "成交清单数量不符")
target = root / "进出场图与成交清单.zip"
with target.open("wb") as raw:
    with io.BufferedWriter(BoundedFile(raw, budget, target), buffer_size=1024 * 1024) as sink:
        with ZipFile(sink, "w", ZIP_DEFLATED, compresslevel=6) as archive:
            for path in paths:
                archive.write(path, path.name)
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
