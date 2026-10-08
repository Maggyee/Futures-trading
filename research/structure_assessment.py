"""Apply the finite frozen gates; failed individual stages cannot be combined."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import file_sha256
from .frequency_assessment import compare_entries, diagnostics, opportunity
from .frequency_followup import require
from .optimization_assessment import promotion, read_scenario, totals
from .reporting import write_json
from .storage import SpaceBudget
from .structure_followup import PLAN


def assess(final=False):
    plan = json.loads(PLAN.read_text())
    root, scenarios, groups = Path(plan["output"]), [], {}
    for variant in plan["variants"]:
        if not all((root/(m+"_"+variant+"_latest.json")).exists() for m in plan["baselines"]):
            continue
        members = []
        for month in plan["baselines"]:
            s = read_scenario(PLAN, plan, month, variant)
            run = Path(s["directory"])
            proof = json.loads((run/plan["audit_filename"]).read_text())
            require(proof["status"] == "passed", "本轮独立审计尚未通过")
            s.update(paths=proof["paths"],funnel=s["summary"]["signal_funnel"],
                     diagnostics=diagnostics(run),audit_status="passed")
            for name in (plan["audit_filename"],"confirmation_setups.csv.gz","confirmed_channels.csv.gz","signals.csv.gz","events.csv.gz","orders.csv.gz"):
                if (run/name).exists():
                    s["source_hashes"][name] = file_sha256(run/name)
            members.append(s)
        groups[variant] = members
        scenarios.extend(members)
    require("control" in groups,"先完成全部原策略对照")
    if final:
        require(all(v in groups for v in plan["order"]),"独立阶段尚未完成")
    control = groups["control"]
    aggregate, changes, decisions = {}, {}, {}
    for variant, members in groups.items():
        trades = [t for s in members for t in s["trades"]]
        aggregate[variant] = totals(members) | {
            "active_days":len({t["entry_time"][:10] for t in trades}),
            "contract_day_directions":len({opportunity(t) for t in trades}),
            "average_holding_minutes":sum(float(t["holding_minutes"]) for t in trades)/len(trades) if trades else 0}
        if variant != "control":
            changes[variant] = compare_entries(control,members)
    for variant, members in groups.items():
        if variant == "control":
            continue
        is_entry = variant.startswith("confirmation")
        decision = promotion(control,members,aggregate["control"]["trade_count"]+1 if is_entry else 0)
        if is_entry:
            decision["checks"].update({
                "new_contract_day_directions":changes[variant]["new_contract_day_directions"] >= plan["entry_minimum_new_contract_day_directions"],
                "active_days_not_worse":aggregate[variant]["active_days"] >= aggregate["control"]["active_days"]})
            decision["passed"] = all(decision["checks"].values())
        if variant == "confirmation_structure":
            decision["against_individuals"] = {v:promotion(groups[v],members,aggregate[v]["trade_count"])
                                                for v in ("confirmation","structure")}
            decision["passed"] &= all(v["passed"] for v in decision["against_individuals"].values())
        decisions[variant] = decision
    eligible = all(decisions.get(v,{}).get("passed",False) for v in ("confirmation","structure"))
    combination = ("passed" if decisions.get("confirmation_structure",{}).get("passed") else
                   "rejected" if "confirmation_structure" in groups else "pending" if eligible else "not_eligible")
    selected = ("confirmation_structure" if combination == "passed" else
                "confirmation" if decisions.get("confirmation",{}).get("passed") else
                "structure" if decisions.get("structure",{}).get("passed") else "control")
    assessment = {
        "created":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "plan":str(PLAN),"plan_sha256":file_sha256(PLAN),"status":"completed" if final else "in_progress",
        "labels":{v:plan["labels"][v] for v in groups},"totals":aggregate,"promotion":decisions,
        "changes":changes,"selected":selected,"combination_status":combination,
        "research_days":sum(s["metrics"]["daily_count"] for s in control),
        "criteria":plan["promotion"],"entry_minimum_trades":aggregate["control"]["trade_count"]+1,
        "sample_status":plan["sample_status"],"locked_test_read":False,
        "new_holdout_results_claimed":False,"live_trading_changed":False,
        "scenarios":[{k:s[k] for k in ("month","variant","directory","window","metrics","trades","paths","funnel","diagnostics","audit_status","source_hashes")} for s in scenarios]}
    budget = SpaceBudget(plan["budget"])
    write_json(root/"assessment.json",assessment,budget)
    if final:
        require(combination != "pending","满足资格的组合尚未完成")
        cfg = json.loads((Path(groups[selected][0]["directory"])/"config_snapshot.json").read_text())
        write_json(root/"research_candidate.json",{
            "variant":selected,"label":plan["labels"][selected],"strategy":cfg["strategy"],"risk":cfg["risk"],
            "window_configs":{s["month"]:str(Path(s["directory"])/"config_snapshot.json") for s in groups[selected]},
            "assessment_sha256":file_sha256(root/"assessment.json"),"research_only":True,
            "default_strategy_changed":False,"live_trading_changed":False,"locked_test_read":False,
            "sample_status":plan["sample_status"]},budget)
    print(json.dumps({"phase":"assessment","selected":selected,"completed_windows":len(scenarios),
                      "passed":[v for v,c in decisions.items() if c["passed"]]},ensure_ascii=False),flush=True)
    return assessment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final",action="store_true")
    assess(final=parser.parse_args().final)
