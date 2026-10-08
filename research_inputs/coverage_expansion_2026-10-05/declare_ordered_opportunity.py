"""Freeze a new finite round against the already-passed stop-reentry controls."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from research.data import file_sha256
from research.storage import SpaceBudget, write_bounded_json

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[1]
TARGET = HERE / "ordered_opportunity_plan.json"


def main():
    if TARGET.exists():
        raise RuntimeError("冻结声明已存在，不覆盖")
    previous = json.loads((HERE / "opportunity_followup_plan.json").read_text())
    old_root = Path(previous["output"]).parent / "frequency_followup"
    baselines = {}
    for month in previous["baselines"]:
        record = json.loads((old_root / (month + "_stop_reentry_latest.json")).read_text())
        run = Path(record["directory"])
        audit = json.loads((run / "independent_coverage_audit.json").read_text())
        if record["status"] != "completed" or audit["status"] != "passed":
            raise RuntimeError("止损后禁入对照尚未通过核验")
        baselines[month] = {"directory": str(run), **{
            key: file_sha256(run / name) for key, name in (
                ("config_sha256", "config_snapshot.json"), ("trades_sha256", "trades.csv.gz"),
                ("manifest_sha256", "manifest.json"), ("audit_sha256", "independent_coverage_audit.json"))}}
    pool = {"candidate_pool": {"policy": "executable_affordable_cost",
            "preserve_original_rank": True, "refresh": "completed_minute",
            "preserve_held_and_pending_slots": True}}
    dual = {"trend_entry": copy.deepcopy(previous["variants"]["trend"]["trend_entry"]),
            "dual_entry": {"priority": "direct", "shared_capital": True,
                           "consume_pullback_once": True}}
    plan = {
        "schema": 1, "kind": "sequential_optimization_diagnostic",
        "created_utc": datetime.now(timezone.utc).isoformat(), "baselines": baselines,
        "order": ["control", "pool", "dual"],
        "variants": {"control": {}, "pool": pool, "dual": dual, "pool_dual": pool | dual},
        "labels": {"control": "已通过的止损后禁入对照", "pool": "可承担且成本合格候选池",
                   "dual": "原突破＋趋势回踩双通道", "pool_dual": "候选池＋双通道"},
        "output": str(ROOT / "research_outputs/coverage_expansion_2026-10-05/ordered_opportunity"),
        "audit_filename": "independent_ordered_opportunity_audit.json",
        "budget": previous["budget"], "k": 2, "maximum_runs": 12,
        "promotion": previous["promotion"],
        "selection": {
            "priority": ["pool", "dual"],
            "combination": "only if both independently pass; must pass against control and both individuals",
            "failure": "retain passed stop-reentry control, preserve failures, do not retune frozen recipes",
        },
        "candidate_scope": "Use only current completed close, causal ATR and execution inputs available by the decision time. Skip unavailable, unaffordable minimum-lot or cost/ATR-ineligible candidates; retain original R8 ordering, K=2 and held/pending slots. Do not select by eventual market signals or profit.",
        "dual_scope": "Preserve all original breakout conditions; add the prior frozen 5m/15m trend and completed MA10 pullback recipe as a supplement. Both use shared capacity and original stop-reentry gate. Same contract/minute triggers prefer breakout; consume each accepted pullback once. Direct edge detection remains independent of pullback qualification.",
        "protection_stage": "Retain and independently audit original 1R cost-covered break-even, hard stop, 2ATR original-target trail, volume, MA40 and session exits on both channels. Do not repeat failed 1.5R or two-bar MA40 experiments.",
        "sample_status": "retrospective_diagnostic_on_already_inspected_windows",
        "locked_test_read": False, "live_trading_changed": False, "parameter_search": False,
        "retune_after_result": False, "new_holdout_results_claimed": False,
        "prior_assessments": {str(p): file_sha256(p) for p in (
            old_root / "assessment.json", Path(previous["output"]) / "assessment.json")},
    }
    budget = SpaceBudget(plan["budget"])
    budget.check(TARGET, reserve=20000)
    Path(plan["output"]).mkdir(exist_ok=True)
    write_bounded_json(TARGET, plan, budget)
    print(json.dumps({"plan": str(TARGET), "sha256": file_sha256(TARGET),
                      "maximum_runs": plan["maximum_runs"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
