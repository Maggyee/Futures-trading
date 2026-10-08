"""Apply the finite frozen gates; failed individual stages cannot be combined."""

import argparse
import json
from collections import Counter, defaultdict
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


def research_checks(metrics, criteria):
    """Absolute research budgets also apply to a formerly empty window."""
    return {"drawdown_within_budget": all(m["max_drawdown"] <= criteria["max_window_drawdown_cny"]+1e-6 for m in metrics),
            "loss_within_budget": all(m["net_profit"] >= -criteria["max_window_loss_cny"]-1e-6 for m in metrics),
            "enough_trades_for_further_review": sum(m["trade_count"] for m in metrics) >= criteria["minimum_trades_for_further_review"]}


def assess_layers(plan_path, final=False):
    from .coverage_audit import rows
    from .reporting import write_csv

    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    root, groups, ledgers, shape_sets = Path(plan["output"]), defaultdict(list), [], {}
    for month in sorted(plan["baselines"]):
        for variant in plan["order"]:
            pointer = root/(month+"_"+variant+"_latest.json")
            if not pointer.exists():
                require(not final,"声明的回放尚未完成")
                continue
            scenario = read_scenario(plan_path,plan,month,variant)
            run = Path(scenario["directory"])
            proof = json.loads((run/plan["audit_filename"]).read_text())
            require(proof["status"] == "passed", "独立审计未通过")
            cfg = json.loads((run/"config_snapshot.json").read_text())
            rules = {(r["trading_day"],r["contract"]):r for r in cfg["execution"]["qualification"]["rules"]}
            channel_net, product_net = defaultdict(float), defaultdict(float)
            stress = 0.
            for trade in scenario["trades"]:
                quantity, entry, exit_price = int(trade["quantity"]), float(trade["entry_price"]), float(trade["exit_price"])
                sign = 1 if trade["direction"] == "LONG" else -1
                protection = json.loads(trade["entry_protection"])
                trailing = json.loads(trade["trailing_exit"])
                allocation = json.loads(trade["entry_allocation"])
                meta = rules[(trade["entry_time"][:10],trade["contract"])]
                tick, value, net = meta["tick_size"], meta["value_per_price"], float(trade["net_pnl"])
                channel = json.loads(trade["entry_snapshot"]).get("entry_channel","legacy")
                distance = abs(entry-float(trade["stop_price"]))
                channel_net[channel] += net
                product_net[trade["product"].upper()] += net
                extra_cost = 2*tick*value*quantity
                stress += net-extra_cost
                ledgers.append({"month":month,"variant":variant,"contract":trade["contract"],"direction":trade["direction"],
                    "entry_time":trade["entry_time"],"exit_time":trade["exit_time"],"channel":channel,"quantity":quantity,
                    "unit_price_points":sign*(exit_price-entry),"net_per_lot":net/quantity,
                    "net_initial_price_r":net/(quantity*distance*value),"net_budget_r":net/allocation["planned_risk"],
                    "net_cny":net,"initial_risk_distance":distance,
                    "breakeven_activation_distance":abs(trailing["breakeven_activation_price"]-entry),
                    "trailing_activation_distance":abs(float(trade["target_price"])-entry),
                    "trailing_atr_multiple":trailing["atr_multiple"],
                    "risk_budget_utilization":allocation["planned_risk"]/allocation["single_trade_budget"],
                    "profit_reference":protection.get("profit_protection_reference"),
                    "fixed_path_extra_tick_net":net-extra_cost})
            events = list(rows(run/"confirmation_events.csv.gz"))
            patterns = {(month,r["date"],r["contract"],r["direction"],r["kind"],r["setup_id"]) for r in events if r["action"] == "formed"}
            shape_sets.setdefault(variant,set()).update(patterns)
            diagnostics_rows = list(rows(run/"opportunity_diagnostics.csv.gz"))
            counters, account_keys, cost_keys = Counter(), set(), set()
            for row in diagnostics_rows:
                key = row["date"],row["contract"],row["direction"],row["channel"]
                if row["market_qualified"] == "True":
                    counters["market_qualified_observations"] += 1
                    if row["cost_pass"] == "False":
                        counters["cost_rejected_observations"] += 1
                        cost_keys.add(key)
                    if row.get("current_quantity_capacity") == "0":
                        counters["account_zero_capacity_observations"] += 1
                        account_keys.add(key)
                if json.loads(row["execution_rejections"]):
                    counters["execution_data_rejected_observations"] += 1
            groups[variant].append(scenario | {"channel_net":dict(channel_net),"product_net":dict(product_net),
                "fixed_path_extra_tick_net":stress,"lifecycle":dict(Counter(r["action"]+":"+r["reason"] for r in events)),
                "diagnostics":dict(counters),"account_rejected_coarse_opportunities":len(account_keys),
                "cost_rejected_coarse_opportunities":len(cost_keys)})
    aggregate, decisions, changes = {}, {}, {}
    control = groups["control"]
    for variant,members in groups.items():
        channel_net, product_net, reasons = defaultdict(float), defaultdict(float), Counter()
        for scenario in members:
            for k,v in scenario["channel_net"].items():
                channel_net[k] += v
            for k,v in scenario["product_net"].items():
                product_net[k] += v
            reasons.update(scenario["lifecycle"])
        aggregate[variant] = totals(members) | {"channel_net":dict(channel_net),"product_net":dict(product_net),
            "non_lc_net":sum(v for k,v in product_net.items() if k != "LC"),
            "lifecycle":dict(reasons),"deduplicated_shapes":len(shape_sets[variant]),
            "fixed_path_extra_tick_net":sum(s["fixed_path_extra_tick_net"] for s in members),
            "minimum_window_net":min(s["metrics"]["net_profit"] for s in members)}
        checks = research_checks([s["metrics"] for s in members],plan["research_criteria"])
        decisions[variant] = {"engineering":"passed", "research_checks":checks,
                             "status":"requires_unseen_data" if all(checks.values()) else "insufficient_evidence_or_budget_failure",
                             "automatic_promotion":False}
        if variant != "control":
            changes[variant] = compare_entries([s for s in control if s["month"] in {m["month"] for m in members}],members)
    shape_changes = {}
    for before,after in (("bucket_confirmation","lifetime"),("lifetime","pullback_recovery")):
        if before in shape_sets and after in shape_sets:
            shape_changes[after] = {"compared_with":before,
                "new":[list(k) for k in sorted(shape_sets[after]-shape_sets[before])],
                "lost":[list(k) for k in sorted(shape_sets[before]-shape_sets[after])],
                "unit":"deduplicated timestamped patterns, not statistically independent observations"}
    assessment = {"status":"completed" if final else "in_progress","plan":str(plan_path),"plan_sha256":file_sha256(plan_path),
        "criteria":plan["research_criteria"],"totals":aggregate,"decisions":decisions,"entry_changes":changes,"shape_changes":shape_changes,
        "sample_status":plan["sample_status"],"locked_test_read":False,"new_holdout_results_claimed":False,
        "automatic_promotion":False,"research_days":sum(s["metrics"]["daily_count"] for s in control),
        "scenarios":[{k:s[k] for k in ("month","variant","directory","metrics","source_hashes","diagnostics",
                    "account_rejected_coarse_opportunities","cost_rejected_coarse_opportunities")} for members in groups.values() for s in members]}
    budget = SpaceBudget(plan["budget"])
    write_json(root/"assessment.json",assessment,budget)
    write_csv(root/"trade_units.csv",ledgers,budget)
    lines = ["# 环境、形态、执行与风险规则复核", "",f"状态：{assessment['status']}；覆盖{assessment['research_days']}个已查看开发交易日。",
        "各窗口分别重置100万元，合计金额不是连续账户曲线。原失败实验结论保留；9月24—30日继续锁定。",
        "", "| 方案 | 成交 | 净额（元） | 去掉最佳一笔 | 非LC净额 | 最大窗口回撤 |", "|---|---:|---:|---:|---:|---:|"]
    for variant,total in aggregate.items():
        lines.append(f"| {plan['labels'][variant]} | {total['trade_count']} | {total['net']:.2f} | {total['without_best_trade']:.2f} | {total['non_lc_net']:.2f} | {total['max_window_drawdown']:.2f} |")
    lines += ["", "| 方案 | 窗口 | 成交 | 净额（元） | 回撤（元） |", "|---|---|---:|---:|---:|"]
    for variant,members in groups.items():
        for s in members:
            m = s["metrics"]
            lines.append(f"| {plan['labels'][variant]} | {s['month']} | {m['trade_count']} | {m['net_profit']:.2f} | {m['max_drawdown']:.2f} |")
    lines += ["", "| 方案 | 通道扣费后净贡献（元） | 去掉最佳两笔 | 最佳一笔占盈利净额之和 |", "|---|---|---:|---:|"]
    for variant,total in aggregate.items():
        contributions = "；".join(k+"="+f"{v:.2f}" for k,v in total["channel_net"].items())
        share = total["best_share_of_winning_net"]
        lines.append(f"| {plan['labels'][variant]} | {contributions} | {total['without_two_best_trades']:.2f} | {share:.2%} |" if share is not None else
                     f"| {plan['labels'][variant]} | {contributions} | {total['without_two_best_trades']:.2f} | 无盈利交易 |")
    lines += ["", "旧确认方案、有效期修正和回踩恢复方案的每次形成及终态均写入 confirmation_events.csv.gz；新增/丢失形态清单在 assessment.json。形态时间去重及合约/日/方向汇总不代表独立统计样本。",
        "", "| 确认方案 | 去重形态 | 终态原因 |", "|---|---:|---|"]
    for variant in ("bucket_confirmation","lifetime","pullback_recovery"):
        if variant in aggregate:
            total = aggregate[variant]
            reasons = "；".join(k+"="+str(v) for k,v in total["lifecycle"].items() if k.startswith(("confirmed:","cancelled:")))
            lines.append(f"| {plan['labels'][variant]} | {total['deduplicated_shapes']} | {reasons} |")
    lines += ["", "opportunity_diagnostics.csv.gz记录成本/1m ATR、成本/5m ATR、成本/原训练规则与因果下限定义的目标空间、成本/初始风险，以及最小开仓数量的最低资金需求和当前资金拒绝。目标空间只是事先规则的价格尺度，不是对未来有利空间的预测；本轮未更改成本门槛。资金需求是空仓下限，当前容量仍受预约、分组和每日开仓限制；风险额度取初始资金与当前权益的较小值，盈利不能解除初始资金不足。",
        "", "trade_units.csv同时列出单手价格净表现、初始价格风险R、含成本预算R、真实整数手数净额、保本与追踪启动距离和风险预算利用率。额外双边1跳只按原账本扣减成本并保留原费用和路径，不能解释为重新撮合的压力回测。",
        "", "工程正确性独立验收；研究使用预声明的每窗口10000元绝对回撤/亏损预算、集中度、成本及窗口表现。8月零交易不要求新方案回撤也为零。20笔仅是继续复核的最低样本门槛；本轮全部属于开发样本，任何方案均不自动升级。"]
    (root/"report.md").write_text("\n".join(lines)+"\n")
    print(json.dumps({"phase":"layer_assessment","status":assessment["status"],"windows":len(assessment["scenarios"]),
                      "totals":{v:{"trades":t["trade_count"],"net":round(t["net"],2)} for v,t in aggregate.items()}},ensure_ascii=False),flush=True)
    return assessment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final",action="store_true")
    parser.add_argument("--plan",type=Path)
    args = parser.parse_args()
    if args.plan:
        assess_layers(args.plan,final=args.final)
    else:
        assess(final=args.final)
