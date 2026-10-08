"""One predeclared coverage expansion; no parameter fitting or live execution."""

import argparse
import copy
import fcntl
import json
from pathlib import Path

from .acquisition import acquire_month
from .config import ResearchError, digest, read_config
from .data import file_sha256, load_data
from .experiments import run_one
from .prepared_entries import PreparedEntries
from .prepared_review import prepare_review
from .reporting import write_json
from .storage import SpaceBudget


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "research_inputs/coverage_expansion_2026-10-05/plan.json"
ORIGINAL = ROOT / "research_outputs/2026-09/lc_history_baseline_k2/run_0001_21c90080fd67"


def declaration(path):
    path = Path(path).resolve()
    plan = json.loads(path.read_text())
    parent = Path(plan["parent_run"])
    if file_sha256(parent / "config_snapshot.json") != plan["parent_config_sha256"]:
        raise ResearchError("扩展评估的父策略已改变")
    return path, plan, json.loads((parent / "config_snapshot.json").read_text())


def storage_config(cfg, plan, name):
    cfg["storage"] = {"shared_root": str(Path(plan["budget"]["roots"][0]) / "datasets"),
                      "compact_results": True, "audit_export_normalized": False,
                      "indicator_cache_root": str(Path(plan["budget"]["roots"][1]) / name / "indicator_cache"),
                      "budget": plan["budget"]}


def september(path, k):
    path, plan, cfg = declaration(path)
    if k not in plan["candidate_k"]:
        raise ResearchError("K不在预先声明的对照内")
    cfg["strategy"]["k"] = k
    cfg["baseline_expectation"]["strategy"]["k"] = k
    storage_config(cfg, plan, "2026-09")
    cfg["development_review"] = {"step": "coverage_candidate_expansion", "parent": plan["parent_run"],
        "plan_sha256": file_sha256(path), "sample_status": "development_sample_repeatedly_inspected",
        "parameter_search": False, "locked_test_read": False}
    data, features, evidence = prepare_review(ORIGINAL, cfg)
    entries = PreparedEntries(plan["parent_run"], cfg, evidence, allow_candidate_expansion=k != 2)
    output = Path(plan["budget"]["roots"][1]) / "2026-09" / ("k" + str(k))
    print(json.dumps({"phase": "prepared", "month": "2026-09", "k": k, "bars": len(data.bars)}), flush=True)
    run, result = run_one(data, cfg, output, cfg["splits"]["validation"],
                         prepared_features=features, prepared_entries=entries)
    budget = SpaceBudget(plan["budget"])
    write_json(run / "prepared_source_review.json", evidence, budget)
    write_json(run / "prepared_entries_review.json", entries.evidence, budget)
    return save_summary(plan, "2026-09", k, run, result)


def save_summary(plan, month, k, run, result):
    if result["status"] != "completed":
        raise ResearchError("扩展回放未完成：" + str(result.get("error")))
    summary = {"month": month, "k": k, "directory": str(run), "metrics": result["metrics"],
               "window": json.loads((run / "manifest.json").read_text())["window"],
               "status": "completed", "locked_test_read": False}
    output = Path(plan["budget"]["roots"][1])
    write_json(output / (month + "_k" + str(k) + "_latest.json"), summary, SpaceBudget(plan["budget"]))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def acquire(path, requests):
    path, plan, _ = declaration(path)
    if not 1 <= requests <= plan["request_limit_per_invocation"]:
        raise ResearchError("公开下载单批不能超过预先声明的请求上限")
    root = path.parent
    with (root / "acquire.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResearchError("已有公开数据下载进行中，等待下一轮") from None
        for month in plan["months"]:
            target = root / month
            progress_path = target / "progress.json"
            progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
            if progress.get("status") in {"data_download_completed", "completed_with_gaps"}:
                continue
            attempts = target / "download_attempts.jsonl"
            count = len(attempts.read_text().splitlines()) if attempts.exists() else 0
            if count >= plan["monthly_request_limit"]:
                raise ResearchError("达到预先声明的单月请求上限，保留缺口")
            return acquire_month(target / "acquire.json", request_limit=min(requests, plan["monthly_request_limit"] - count))
    return {"status": "declared_acquisition_completed", "months": plan["months"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["acquire", "september", "documents", "prepare", "run"])
    parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--month", choices=["2026-07", "2026-08"])
    parser.add_argument("--requests", type=int, default=400)
    args = parser.parse_args()
    if args.action == "september":
        september(args.plan, args.k)
    elif args.action == "acquire":
        try:
            result = acquire(args.plan, args.requests)
        except ResearchError as exc:
            if "已有公开数据" not in str(exc) and "每日预算" not in str(exc):
                raise
            result = {"status": "deferred", "reason": str(exc)}
        print(json.dumps({k:result[k] for k in ("status", "storage", "months", "reason") if k in result}, ensure_ascii=False))
    elif args.action == "documents":
        from .historical_coverage import document_batch
        document_batch(args.plan, args.month)
    else:
        from .historical_coverage import prepare, run
        (prepare if args.action == "prepare" else run)(args.plan, args.month, args.k)


if __name__ == "__main__":
    main()
