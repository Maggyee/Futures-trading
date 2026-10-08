"""Apply the frozen promotion criteria to audited portfolio replays."""

import argparse
import json
import math
from pathlib import Path

from .config import ResearchError
from .coverage_audit import rows
from .data import file_sha256
from .optimization_declaration import validate_optimization
from .optimization_audit import audit_source_archive
from .reporting import write_json
from .storage import SpaceBudget


def read_scenario(plan_path, plan, month, variant):
    latest = Path(plan["output"]) / (month + "_" + variant + "_latest.json")
    if not latest.exists():
        raise ResearchError("等待完整优化回放：" + latest.name)
    record = json.loads(latest.read_text())
    run = Path(record["directory"])
    cfg = json.loads((run / "config_snapshot.json").read_text())
    validate_optimization(cfg)
    if cfg["optimization_review"] != {"plan": str(Path(plan_path).resolve()), "plan_sha256": file_sha256(plan_path),
            "month": month, "variant": variant, "sample_status": plan["sample_status"], "locked_test_read": False}:
        raise ResearchError("优化结果的声明来源不符")
    review = json.loads((run / "independent_coverage_audit.json").read_text())
    checks = review.get("optimization_checks", {})
    required = ["journal"] + (["pullback"] if variant == "pullback" else []) + (["afternoon"] if variant == "afternoon" else [])
    if record["status"] != "completed" or review["status"] != "passed" or any((checks.get(key) or {}).get("status") != "passed" for key in required):
        raise ResearchError("优化结果尚未通过完整独立审计")
    summary = json.loads((run / "summary.json").read_text())
    source_archive = audit_source_archive(run)
    trades = list(rows(run / "trades.csv.gz"))
    if len(trades) != record["metrics"]["trade_count"] or summary["metrics"] != record["metrics"]:
        raise ResearchError("最新记录与逐笔结果不符")
    net = sum(float(t["net_pnl"]) for t in trades)
    if not math.isclose(net, record["metrics"]["net_profit"], abs_tol=1e-6):
        raise ResearchError("逐笔净收益与汇总不符")
    return {**record, "trades": trades, "summary": summary, "audit": review, "source_archive_audit": source_archive,
            "source_hashes": {name: file_sha256(run/name) for name in ("config_snapshot.json", "manifest.json", "trades.csv.gz", "summary.json", "independent_coverage_audit.json", "source_snapshot.tar.gz")}}


def totals(scenarios):
    trades = [t for scenario in scenarios for t in scenario["trades"]]
    net = sum(float(t["net_pnl"]) for t in trades)
    best = max((float(t["net_pnl"]) for t in trades), default=0)
    positive = sum(max(0, float(t["net_pnl"])) for t in trades)
    gross = sum(float(t["gross_pnl"]) for t in trades)
    return {"net": net, "gross": gross, "fees": sum(float(t["fees"]) for t in trades),
            "trade_count": len(trades), "win_count": sum(float(t["net_pnl"]) > 0 for t in trades),
            "best_trade": best, "without_best_trade": net-best,
            "without_two_best_trades": net-sum(sorted((float(t["net_pnl"]) for t in trades), reverse=True)[:2]),
            "best_share_of_winning_net": best/positive if positive > 0 else None,
            "quick_trades": sum(float(t["holding_minutes"]) <= 5 for t in trades),
            "slippage_in_gross": True, "independent_windows": True,
            "max_window_drawdown": max(s["metrics"]["max_drawdown"] for s in scenarios)}


def promotion(control, candidate, minimum_trades):
    before, after = totals(control), totals(candidate)
    controls = {s["month"]: s for s in control}
    windows = []
    for row in candidate:
        old = controls[row["month"]]
        windows.append({"month": row["month"], "net_delta": row["metrics"]["net_profit"]-old["metrics"]["net_profit"],
                        "drawdown_delta": row["metrics"]["max_drawdown"]-old["metrics"]["max_drawdown"],
                        "trade_delta": row["metrics"]["trade_count"]-old["metrics"]["trade_count"]})
    checks = {"total_net_improves": after["net"] > before["net"] + 1e-6,
              "each_window_net_not_worse": all(w["net_delta"] >= -1e-6 for w in windows),
              "each_window_drawdown_not_worse": all(w["drawdown_delta"] <= 1e-6 for w in windows),
              "without_best_trade_net_not_worse": after["without_best_trade"] >= before["without_best_trade"] - 1e-6,
              "minimum_trade_count": after["trade_count"] >= minimum_trades}
    return {"passed": all(checks.values()), "checks": checks, "windows": windows,
            "total_net_delta": after["net"]-before["net"],
            "without_best_trade_delta": after["without_best_trade"]-before["without_best_trade"],
            "total_trade_delta": after["trade_count"]-before["trade_count"]}


def assess(plan_path, *, persist=True):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    scenarios = [read_scenario(plan_path, plan, month, variant) for variant in plan["order"] for month in plan["baselines"]]
    control = [s for s in scenarios if s["variant"] == "control"]
    decisions = {variant: promotion(control, [s for s in scenarios if s["variant"] == variant], plan["promotion"]["minimum_trade_count"])
                 for variant in plan["order"] if variant != "control"}
    result = {"plan": str(plan_path), "plan_sha256": file_sha256(plan_path),
              "totals": {variant: totals([s for s in scenarios if s["variant"] == variant]) for variant in plan["order"]},
              "promotion": decisions, "combined_eligible": decisions["cost"]["passed"] and decisions["breakeven"]["passed"],
              "criteria": plan["promotion"], "locked_test_read": False,
              "sample_status": plan["sample_status"], "new_holdout_results_claimed": False,
              "inputs": [{"month": s["month"], "variant": s["variant"], "directory": s["directory"], "hashes": s["source_hashes"]} for s in scenarios]}
    if persist:
        write_json(Path(plan["output"]) / "assessment.json", result, SpaceBudget(plan["budget"]))
    return result, scenarios


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(Path(__file__).resolve().parents[1]/"research_inputs/coverage_expansion_2026-10-05/optimization_plan.json"))
    args = parser.parse_args()
    result, _ = assess(args.plan)
    print(json.dumps({"totals": result["totals"], "promotion": result["promotion"], "combined_eligible": result["combined_eligible"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
