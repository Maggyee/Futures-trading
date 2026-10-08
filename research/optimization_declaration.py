"""Verify the exact predeclared changes against immutable coverage controls."""

import json
from pathlib import Path

from .config import ResearchError
from .data import file_sha256


def validate_optimization(cfg):
    review = cfg.get("optimization_review", {})
    plan_path = Path(review.get("plan", ""))
    if not plan_path.is_file() or file_sha256(plan_path) != review.get("plan_sha256"):
        raise ResearchError("优化声明缺失或指纹改变")
    plan = json.loads(plan_path.read_text())
    if plan.get("kind") != "sequential_optimization_diagnostic" or plan.get("locked_test_read") is not False:
        raise ResearchError("优化必须使用已声明的诊断窗口并保留测试锁")
    record = plan["baselines"].get(review.get("month"))
    if record is None:
        raise ResearchError("优化月份未预先声明")
    run = Path(record["directory"])
    for name, field in (("config_snapshot.json", "config_sha256"),
                        ("trades.csv.gz", "trades_sha256"),
                        ("manifest.json", "manifest_sha256")):
        if file_sha256(run / name) != record[field]:
            raise ResearchError("优化控制来源改变：" + name)
    old = json.loads((run / "config_snapshot.json").read_text())
    variant = review.get("variant")
    if variant == "combined":
        delta = plan["variants"]["cost"] | plan["variants"]["breakeven"]
    elif variant in plan["variants"]:
        delta = plan["variants"][variant]
    else:
        raise ResearchError("优化方案未预先声明")
    if cfg["strategy"] != old["strategy"] | delta:
        raise ResearchError("优化策略改动超出冻结声明")
    for key in ("risk", "metadata", "calendar", "splits", "execution", "calibration_snapshot"):
        if cfg[key] != old[key]:
            raise ResearchError("优化不得改变原控制资料：" + key)
    expected = {key: cfg["strategy"][key] for key in ("k", "entry_mode")}
    if cfg["baseline_expectation"]["strategy"] != expected:
        raise ResearchError("优化入场模式未明确声明")
    return plan, old
