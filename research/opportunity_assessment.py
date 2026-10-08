"""Apply the frozen order and distinguish new opportunities from changed timing."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import file_sha256
from .frequency_assessment import compare_entries, diagnostics, opportunity
from .frequency_followup import require
from .opportunity_followup import PLAN
from .optimization_assessment import promotion, read_scenario, totals
from .reporting import write_json
from .storage import SpaceBudget


def assess(final=False):
    plan = json.loads(PLAN.read_text())
    root, scenarios, groups = Path(plan["output"]), [], {}
    for variant in plan["variants"]:
        pointers = [root / (m + "_" + variant + "_latest.json") for m in plan["baselines"]]
        if not all(p.exists() for p in pointers):
            continue
        members = []
        for month in plan["baselines"]:
            s = read_scenario(PLAN, plan, month, variant)
            proof = json.loads((Path(s["directory"]) / "independent_opportunity_audit.json").read_text())
            require(proof["status"] == "passed", "完整机会审计尚未通过")
            s.update(paths=proof["paths"], funnel=s["summary"]["signal_funnel"],
                     diagnostics=diagnostics(s["directory"]), audit_status=proof["status"])
            for name in ("independent_opportunity_audit.json", "candidate_selection.csv.gz", "exit_confirmation.csv.gz"):
                path = Path(s["directory"]) / name
                if path.exists():
                    s["source_hashes"][name] = file_sha256(path)
            members.append(s)
        groups[variant] = members
        scenarios.extend(members)
    require("control" in groups, "先完成三个对照窗口")
    control = groups["control"]
    if final:
        require(all(v in groups for v in plan["order"]), "独立阶段尚未全部完成")
    original_count = totals(control)["trade_count"]
    decisions = {}
    for variant, group in groups.items():
        if variant == "control":
            continue
        is_entry = variant.startswith(("replacement", "trend"))
        minimum = original_count + 1 if is_entry else 0
        decisions[variant] = promotion(control, group, minimum)
        if variant == "replacement_trend":
            comparisons = {v: promotion(groups[v], group, minimum) for v in ("replacement", "trend")}
            decisions[variant]["against_individual_entries"] = comparisons
            decisions[variant]["passed"] &= all(c["passed"] for c in comparisons.values())
        elif "_" in variant and variant.rsplit("_", 1)[1] in ("breakeven15", "ma40confirm"):
            entry = variant.rsplit("_", 1)[0]
            compare = promotion(groups[entry], group, minimum)
            decisions[variant]["against_selected_entry"] = compare
            decisions[variant]["passed"] &= compare["passed"]
    entry = next((v for v in ("replacement", "trend") if decisions.get(v, {}).get("passed")), None)
    if decisions.get("replacement_trend", {}).get("passed"):
        entry = "replacement_trend"
    exit_name = next((v for v in ("breakeven15", "ma40confirm") if decisions.get(v, {}).get("passed")), None)
    combination = entry + "_" + exit_name if entry and exit_name else None
    combined_status = ("passed" if decisions.get(combination, {}).get("passed") else
                       "rejected" if combination in groups else
                       "pending" if combination else "not_eligible")
    selected = combination if combined_status == "passed" else entry or exit_name or "control"
    labels = dict(plan["labels"])
    labels["replacement_trend"] = "候选补位＋高周期趋势回踩"
    for v in groups:
        if v not in labels:
            a,b = v.rsplit("_",1)
            labels[v] = labels[a] + "＋" + labels[b]
    aggregate, changes = {}, {}
    for variant, group in groups.items():
        trades = [t for s in group for t in s["trades"]]
        aggregate[variant] = totals(group) | {
            "active_days": len({t["entry_time"][:10] for t in trades}),
            "contract_day_directions": len({opportunity(t) for t in trades}),
            "average_holding_minutes": sum(float(t["holding_minutes"]) for t in trades) / len(trades) if trades else 0,
        }
        if variant != "control":
            changes[variant] = compare_entries(control, group)
    assessment = {
        "created": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "plan": str(PLAN), "plan_sha256": file_sha256(PLAN),
        "status": "completed" if final else "in_progress",
        "labels": {v: labels[v] for v in groups}, "totals": aggregate,
        "promotion": decisions, "selected_entry": entry, "selected_exit": exit_name,
        "conditional_combination": combination, "combination_status": combined_status,
        "selected": selected, "changes": changes,
        "research_days": sum(s["metrics"]["daily_count"] for s in control),
        "criteria": plan["promotion"],
        "entry_minimum_trades": original_count + 1, "exit_minimum_trades": 0,
        "sample_status": plan["sample_status"], "locked_test_read": False,
        "new_holdout_results_claimed": False, "live_trading_changed": False,
        "scenarios": [{k: s[k] for k in ("month", "variant", "directory", "window", "metrics",
                                           "trades", "paths", "funnel", "diagnostics", "audit_status", "source_hashes")}
                      for s in scenarios],
    }
    budget = SpaceBudget(plan["budget"])
    write_json(root / "assessment.json", assessment, budget)
    if final:
        require(combined_status != "pending", "有资格组合尚未完成")
        members = groups[selected]
        cfg = json.loads((Path(members[0]["directory"]) / "config_snapshot.json").read_text())
        write_json(root / "research_candidate.json", {
            "variant": selected, "label": labels[selected], "strategy": cfg["strategy"],
            "risk": cfg["risk"], "window_configs": {s["month"]: str(Path(s["directory"]) / "config_snapshot.json") for s in members},
            "assessment_sha256": file_sha256(root / "assessment.json"),
            "research_only": True, "default_strategy_changed": False,
            "live_trading_changed": False, "locked_test_read": False,
            "sample_status": plan["sample_status"],
        }, budget)
    print(json.dumps({"phase": "assessment", "completed_windows": len(scenarios),
                      "selected": selected, "entry": entry, "exit": exit_name,
                      "passed": [v for v,c in decisions.items() if c["passed"]]}, ensure_ascii=False), flush=True)
    return assessment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", action="store_true")
    assess(final=parser.parse_args().final)
