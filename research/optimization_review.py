"""Run the ordered, frozen optimization diagnostics using the same real data."""

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

from .config import ResearchError, read_config
from .coverage_expansion import ORIGINAL, ROOT
from .data import file_sha256, load_data
from .experiments import run_one
from .optimization_declaration import validate_optimization
from .prepared_entries import PreparedEntries
from .prepared_review import prepare_review
from .reporting import write_json
from .signals import Features
from .storage import SpaceBudget

PLAN = ROOT / "research_inputs/coverage_expansion_2026-10-05/optimization_plan.json"


def configuration(plan_path, month, variant):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    if variant == "combined":
        from .optimization_assessment import assess
        decision, _ = assess(plan_path, persist=False)
        if not decision["combined_eligible"]:
            raise ResearchError("成本与保本未各自通过预定晋级标准，禁止组合实验")
    base = Path(plan["baselines"][month]["directory"])
    if file_sha256(base / "config_snapshot.json") != plan["baselines"][month]["config_sha256"]:
        raise ResearchError("优化控制配置指纹改变")
    cfg = read_config(base / "config_snapshot.json")
    delta = plan["variants"][variant] if variant != "combined" else plan["variants"]["cost"] | plan["variants"]["breakeven"]
    cfg["strategy"].update(copy.deepcopy(delta))
    cfg["baseline_expectation"]["strategy"] = {key: cfg["strategy"][key] for key in ("k", "entry_mode")}
    cfg["optimization_review"] = {"plan": str(plan_path), "plan_sha256": file_sha256(plan_path),
        "month": month, "variant": variant, "sample_status": plan["sample_status"], "locked_test_read": False}
    cfg["storage"]["stream_signals"] = True
    if variant != "control":
        cfg["storage"]["record_unselected_signals"] = False
    validate_optimization(cfg)
    return plan, base, cfg


def run(plan_path, month, variant):
    plan, base, cfg = configuration(plan_path, month, variant)
    window = json.loads((base / "manifest.json").read_text())["window"]
    if window["end"] >= cfg["splits"]["test"]["start"]:
        raise ResearchError("优化诊断禁止读取锁定测试")
    root = Path(plan["output"])
    latest = root / (month + "_" + variant + "_latest.json")
    if latest.exists():
        saved = json.loads(latest.read_text())
        previous = json.loads((Path(saved["directory"]) / "config_snapshot.json").read_text())
        if previous == cfg and saved["status"] == "completed":
            print(json.dumps({"phase": "already_completed", "month": month, "variant": variant, "directory": saved["directory"]}), flush=True)
            return saved
        raise ResearchError("已存在不同配置的优化结果，须另立声明")
    if month == "2026-09":
        data, features, evidence = prepare_review(ORIGINAL, cfg)
    else:
        data = load_data(cfg, cutoff=window["end"])
        expected = json.loads((base / "manifest.json").read_text())["data_fingerprint"]
        if data.fingerprint != expected:
            raise ResearchError("优化行情与原控制的语义指纹不同")
        features = Features(data, cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (features.cache_key + ".jsonl.gz")
        evidence = {"retained_data_fingerprint": data.fingerprint, "cache": str(cache),
                    "cache_key": features.cache_key, "cache_sha256": file_sha256(cache),
                    "source_run": str(base), "locked_test_read": False,
                    "feature_algorithm_and_accessors_unchanged": True}
    print(json.dumps({"phase": "data_ready", "month": month, "variant": variant, "bars": len(data.bars)}), flush=True)
    entries = PreparedEntries(base, cfg, evidence, allow_optimization_overlay=True, allow_selected_projection=True) if variant in {"control", "cost", "breakeven", "combined"} else None
    directory, result = run_one(data, cfg, root / variant / month, window,
        split="retrospective" if month != "2026-09" else "validation", prepared_features=features, prepared_entries=entries)
    if result["status"] != "completed":
        raise ResearchError("优化回放失败：" + result.get("error", "unknown"))
    budget = SpaceBudget(plan["budget"])
    write_json(directory / "prepared_source_review.json", evidence, budget)
    if entries is not None:
        write_json(directory / "prepared_entries_review.json", entries.evidence, budget)
    if variant == "control":
        from .coverage_audit import rows
        if list(rows(directory / "trades.csv.gz")) != list(rows(base / "trades.csv.gz")):
            raise ResearchError("优化控制组未精确复现原成交")
    record = {"month": month, "variant": variant, "directory": str(directory), "parent": str(base),
              "window": window, "status": "completed", "metrics": result["metrics"], "locked_test_read": False}
    write_json(latest, record, budget)
    if hasattr(result["signals"], "discard"):
        result["signals"].discard()
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(PLAN))
    parser.add_argument("--month", choices=["2026-07", "2026-08", "2026-09"])
    parser.add_argument("--variant", choices=["control", "cost", "breakeven", "pullback", "afternoon", "combined"])
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all:
        plan = json.loads(Path(args.plan).read_text())
        root = Path(plan["output"])
        root.mkdir(parents=True, exist_ok=True)
        for variant in plan["order"]:
            for month in (["2026-09", "2026-07", "2026-08"] if variant == "control" else list(plan["baselines"])):
                print(json.dumps({"phase": "starting", "month": month, "variant": variant}), flush=True)
                with (root / (variant + "_" + month + ".log")).open("a") as log:
                    subprocess.run([sys.executable, "-m", "research.optimization_review", "--plan", args.plan,
                                    "--month", month, "--variant", variant], stdout=log, stderr=log, check=True)
                record = json.loads((root / (month + "_" + variant + "_latest.json")).read_text())
                with (root / (variant + "_" + month + "_audit.log")).open("a") as log:
                    subprocess.run([sys.executable, "-m", "research.coverage_audit", "--directory", record["directory"]],
                                   stdout=log, stderr=log, check=True)
                print(json.dumps({"phase": "audited", "month": month, "variant": variant,
                                  "net": record["metrics"]["net_profit"], "trades": record["metrics"]["trade_count"]}), flush=True)
        print(json.dumps({"phase": "all_individual_variants_completed"}), flush=True)
    elif args.month and args.variant:
        run(args.plan, args.month, args.variant)
    else:
        parser.error("需要--all或明确月份和方案")


if __name__ == "__main__":
    main()
