"""Apply the declared follow-up criteria and compare actual entry opportunities."""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .coverage_audit import rows
from .data import file_sha256
from .frequency_followup import PLAN, require
from .optimization_assessment import promotion, read_scenario, totals
from .reporting import write_json
from .storage import SpaceBudget

LABELS = {
    "control": "上一轮组合版",
    "efficiency": "效率下限降至0.35",
    "slope5": "5分钟斜率放宽至Q10—Q90",
    "oi": "关闭净增仓过滤",
    "stop_reentry": "同日同方向止损后禁入",
    "efficiency_reentry": "效率0.35＋止损后禁入",
    "slope5_reentry": "5分钟斜率放宽＋止损后禁入",
    "oi_reentry": "关闭净增仓过滤＋止损后禁入",
}


def entry_key(t):
    return t["contract"], t["direction"], t["entry_time"]


def opportunity(t):
    return t["contract"], t["direction"], t["entry_time"][:10]


def compare_entries(control, candidate):
    old = {entry_key(t): t for s in control for t in s["trades"]}
    new = {entry_key(t): t for s in candidate for t in s["trades"]}
    old_days, new_days = ({opportunity(t) for t in ts.values()} for ts in (old, new))

    def compact(t):
        fields = [
            "id",
            "contract",
            "direction",
            "entry_time",
            "exit_time",
            "exit_reason",
            "quantity",
            "net_pnl",
            "holding_minutes",
        ]
        return {k: t[k] for k in fields}

    added = [
        dict(
            compact(new[k]),
            new_contract_day_direction=opportunity(new[k]) not in old_days,
        )
        for k in sorted(new.keys() - old.keys())
    ]
    removed = [compact(old[k]) for k in sorted(old.keys() - new.keys())]
    shared = [
        {
            "before": compact(old[k]),
            "after": compact(new[k]),
            "net_delta": float(new[k]["net_pnl"]) - float(old[k]["net_pnl"]),
        }
        for k in sorted(old.keys() & new.keys())
    ]
    added_net = sum(float(t["net_pnl"]) for t in added)
    removed_net = sum(float(t["net_pnl"]) for t in removed)
    shared_delta = sum(t["net_delta"] for t in shared)
    require(
        abs(
            added_net
            - removed_net
            + shared_delta
            - (totals(candidate)["net"] - totals(control)["net"])
        )
        < 1e-6,
        "逐笔收益变化归因不闭合",
    )
    return {
        "shared": shared,
        "added": added,
        "removed": removed,
        "added_net": added_net,
        "removed_net": removed_net,
        "shared_net_delta": shared_delta,
        "new_contract_day_directions": len(new_days - old_days),
        "removed_contract_day_directions": len(old_days - new_days),
        "interpretation": "相同合约/方向/成交时间为共同入场；同日改时点不算新增独立机会；各笔并非独立统计样本",
    }


def diagnostics(run):
    counts, rejected = Counter(), Counter()
    opportunities, risk_days = set(), set()
    untriggered = []
    for r in rows(Path(run) / "signals.csv.gz"):
        f = json.loads(r["filters"])
        require(f["candidate"], "不应存在未选中候选观察")
        counts["selected_observations"] += 1
        market_ok = r["execution_pass"] == "True" and all(
            v for k, v in f.items() if k not in {"state", "stop_reentry"}
        )
        if market_ok:
            counts["market_eligible_observations"] += 1
            opportunities.add((r["date"], r["contract"], r["direction"]))
            if not f.get("stop_reentry", True):
                counts["market_eligible_blocked_after_stop"] += 1
        if r["trigger"] == "True":
            counts["entry_trigger"] += 1
            if r["risk_pass"] != "True":
                counts["risk_rejected_triggers"] += 1
                risk_days.add((r["date"], r["contract"], r["direction"]))
                rejected.update(json.loads(r["risk_rejections"]))
        counts["actual_fill"] += r["filled"] == "True"
        if r["all_pass"] == "True" and r["trigger"] == "False":
            untriggered.append({k: r[k] for k in ["time", "contract", "direction"]})
    cancellations = [
        {
            k: r.get(k)
            for k in [
                "time",
                "contract",
                "reason",
                "signal_time",
                "modeled_price",
                "modeled_price_limit",
            ]
        }
        for r in rows(Path(run) / "events.csv.gz")
        if r["action"] == "entry_cancelled"
    ]
    return {
        **counts,
        "market_eligible_contract_day_directions": len(opportunities),
        "risk_rejected_contract_day_directions": len(risk_days),
        "risk_rejections": dict(rejected),
        "fill_cancellation_count": len(cancellations),
        "fill_cancellations": cancellations,
        "qualified_without_new_trigger": untriggered,
    }


def assess(plan_path=PLAN):
    path = Path(plan_path).resolve()
    plan = json.loads(path.read_text())
    output = Path(plan["output"])
    for name in ["intent", "training"]:
        require(file_sha256(plan[name]) == plan[name + "_sha256"], "预声明来源改变")
    variants = list(plan["order"])
    scenarios = []

    def load(variant):
        for month in plan["baselines"]:
            s = read_scenario(path, plan, month, variant)
            run = Path(s["directory"])
            a = json.loads((run / "independent_frequency_audit.json").read_text())
            require(
                a["status"] == "passed" and all(a["exact_csv_matches"].values()),
                "缺少频率审计",
            )
            s.update(
                paths=a["paths"],
                audit_status="passed",
                funnel=s["summary"]["signal_funnel"]["sequential"],
                diagnostics=diagnostics(run),
            )
            s["source_hashes"]["independent_frequency_audit.json"] = file_sha256(
                run / "independent_frequency_audit.json"
            )
            scenarios.append(s)

    for variant in variants:
        load(variant)
    control = [s for s in scenarios if s["variant"] == "control"]
    base_count = totals(control)["trade_count"]
    decisions = {
        v: promotion(
            control,
            [s for s in scenarios if s["variant"] == v],
            0 if v == "stop_reentry" else base_count + 1,
        )
        for v in variants
        if v != "control"
    }
    selected_entry = next(
        (v for v in ["efficiency", "slope5", "oi"] if decisions[v]["passed"]), None
    )
    combination = (
        selected_entry + "_reentry"
        if selected_entry and decisions["stop_reentry"]["passed"]
        else None
    )
    combined_status = "not_eligible"
    if combination:
        paths = [
            output / (m + "_" + combination + "_latest.json") for m in plan["baselines"]
        ]
        if all(p.exists() for p in paths):
            load(combination)
            variants.append(combination)
            decisions[combination] = promotion(
                control,
                [s for s in scenarios if s["variant"] == combination],
                base_count + 1,
            )
            combined_status = (
                "passed" if decisions[combination]["passed"] else "rejected"
            )
        else:
            combined_status = "pending"
    selected = (
        combination
        if combined_status == "passed"
        else selected_entry
        or ("stop_reentry" if decisions["stop_reentry"]["passed"] else "control")
    )
    aggregate = {}
    changes = {}
    for v in variants:
        group = [s for s in scenarios if s["variant"] == v]
        ts = [t for s in group for t in s["trades"]]
        aggregate[v] = {
            **totals(group),
            "active_days": len({t["entry_time"][:10] for t in ts}),
            "contract_day_directions": len({opportunity(t) for t in ts}),
            "average_holding_minutes": sum(float(t["holding_minutes"]) for t in ts)
            / len(ts)
            if ts
            else 0,
        }
        if v != "control":
            changes[v] = compare_entries(control, group)
    result = {
        "created": datetime.now().astimezone().isoformat(),
        "plan": str(path),
        "plan_sha256": file_sha256(path),
        "labels": {v: LABELS[v] for v in variants},
        "totals": aggregate,
        "promotion": decisions,
        "selected_entry": selected_entry,
        "conditional_combination": combination,
        "combination_status": combined_status,
        "selected": selected,
        "changes": changes,
        "criteria": plan["promotion"],
        "locked_test_read": False,
        "sample_status": plan["sample_status"],
        "new_holdout_results_claimed": False,
        "live_trading_changed": False,
        "research_days": sum(s["metrics"]["daily_count"] for s in control),
        "scenarios": [
            {
                k: s[k]
                for k in [
                    "month",
                    "variant",
                    "directory",
                    "window",
                    "metrics",
                    "trades",
                    "paths",
                    "funnel",
                    "diagnostics",
                    "audit_status",
                    "source_hashes",
                ]
            }
            for s in scenarios
        ],
    }
    budget = SpaceBudget(plan["budget"])
    write_json(output / "assessment.json", result, budget)
    if combined_status != "pending":
        selected_runs = [s for s in scenarios if s["variant"] == selected]
        cfg = json.loads(
            (Path(selected_runs[0]["directory"]) / "config_snapshot.json").read_text()
        )
        write_json(
            output / "research_candidate.json",
            {
                "variant": selected,
                "label": LABELS[selected],
                "strategy": cfg["strategy"],
                "risk": cfg["risk"],
                "window_configs": {
                    s["month"]: str(Path(s["directory"]) / "config_snapshot.json")
                    for s in selected_runs
                },
                "assessment_sha256": file_sha256(output / "assessment.json"),
                "research_only": True,
                "default_strategy_changed": False,
                "live_trading_changed": False,
                "locked_test_read": False,
                "sample_status": plan["sample_status"],
            },
            budget,
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(PLAN))
    result = assess(parser.parse_args().plan)
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "totals",
                    "promotion",
                    "selected",
                    "conditional_combination",
                    "combination_status",
                ]
            },
            ensure_ascii=False,
        )
    )
