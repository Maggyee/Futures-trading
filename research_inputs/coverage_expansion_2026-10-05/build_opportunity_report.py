"""Deliver the frozen opportunity experiments, including failures and original chart panels."""

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path

from research.coverage_audit import rows
from research.data import file_sha256
from research.opportunity_followup import PLAN, require
from research.reporting import write_json
from research.storage import SpaceBudget

HERE = Path(__file__).parent
OUTPUT = Path(json.loads(PLAN.read_text())["output"])
CHARTS = OUTPUT / "trade_review"


def missed_control_opportunities(a):
    control = [t for s in a["scenarios"] if s["variant"] == "control" for t in s["trades"]]
    trend = [t for s in a["scenarios"] if s["variant"] == "trend" for t in s["trades"]]
    filled = {(t["contract"], t["direction"], t["entry_time"]) for t in trend}
    needed = {(t["contract"], t["entry_signal_time"]): t for t in control
              if (t["contract"], t["direction"], t["entry_time"]) not in filled}
    result = []
    for s in a["scenarios"]:
        if s["variant"] != "trend":
            continue
        for row in rows(Path(s["directory"]) / "signals.csv.gz"):
            old = needed.get((row["contract"], row["time"]))
            if old is None:
                continue
            flags = json.loads(row["filters"])
            result.append({"contract": old["contract"], "direction": old["direction"],
                "original_entry": old["entry_time"], "original_net_pnl": float(old["net_pnl"]),
                "market_rejections_at_original_signal": [k for k, v in flags.items() if not v and k != "state"],
                "state_allows": flags["state"], "trigger": row["trigger"] == "True",
                "higher_context": json.loads(row["snapshot"])["trend_entry"],
                "actual_pullback": json.loads(row["pullback"]) if row["pullback"] else None})
    return result


def selection_diagnosis(a):
    checks, selected_unknown, changes = Counter(), Counter(), set()
    lower_rank = Counter()
    promoted_trades, examples = [], {}
    for s in a["scenarios"]:
        if s["variant"] != "replacement":
            continue
        cfg = json.loads((Path(s["directory"]) / "config_snapshot.json").read_text())
        ranks = {(r["date"], r["contract"]): r for r in rows(Path(s["directory"]) / "daily_candidates.csv.gz")}
        for row in rows(Path(s["directory"]) / "candidate_selection.csv.gz"):
            for c in json.loads(row["checks"]):
                checks[c["action"]] += 1
                if c["action"] == "skip_zero_capacity":
                    for reason in c["rejections"]:
                        checks["zero_" + reason] += 1
                    product = ranks[(row["date"], c["contract"])]["product"]
                    if product not in examples:
                        risk, strategy = cfg["risk"], cfg["strategy"]
                        fraction = risk["group_fractions"][row["group"]]
                        multiplier = c["tick_size"] * c["value_per_price"]
                        minimum_risk = c["minimum_open_lots"] * (c["stop_loss_ticks"] * multiplier +
                            risk["cost_buffer_multiple"] * (c["roundtrip_fees_per_lot"] + 2 * strategy["slippage_ticks"] * multiplier))
                        minimum_margin = c["minimum_open_lots"] * c["price"] * c["value_per_price"] * c["margin_rate"]
                        empty_account_capital = max(minimum_risk / risk["trade_risk_fraction"],
                            minimum_risk / (risk["portfolio_risk_fraction"] * fraction),
                            minimum_margin / (risk["margin_fraction"] * fraction))
                        examples[product] = {"product": product, "contract": c["contract"], "time": row["time"],
                            "group": row["group"], "minimum_open_lots": c["minimum_open_lots"],
                            "minimum_planned_risk": minimum_risk, "minimum_margin": minimum_margin,
                            "equity": float(row["equity"]), "reasons": c["rejections"],
                            "capital_for_minimum_size_without_other_positions": empty_account_capital}
                elif c["action"] == "select" and not c["assessed"]:
                    selected_unknown[c["reason"]] += 1
                if c["action"] == "select" and c["rank"] > 2:
                    changes.add((row["date"], c["contract"], row["direction"]))
        promoted_trades.extend(t for t in s["trades"] if int(t["rank"]) > 2)
        for row in rows(Path(s["directory"]) / "signals.csv.gz"):
            if int(row["rank"]) <= 2:
                continue
            flags = json.loads(row["filters"])
            lower_rank["observations"] += 1
            lower_rank["execution_available"] += row["execution_pass"] == "True"
            lower_rank["all_market_pass"] += row["execution_pass"] == "True" and all(v for k, v in flags.items() if k != "state")
            for key, passed in flags.items():
                if not passed and key != "state":
                    lower_rank["rejected_" + key] += 1
    return {"checks": dict(checks), "unknown_retained": dict(selected_unknown),
            "promoted_contract_day_directions": len(changes),
            "promoted_fills": promoted_trades,
            "lower_rank_observations": dict(lower_rank),
            "zero_capacity_examples": list(examples.values()),
            "capital_estimates_are_capacity_only_not_profit_or_funding_recommendations": True,
            "counts_include_repeated_minutes": True}


def findings(a, selection, missed):
    selected = a["selected"]
    before, after = a["totals"]["control"], a["totals"][selected]
    passed = [a["labels"][v] for v, p in a["promotion"].items() if p["passed"]]
    notes = [
        f"按冻结顺序完成{len(a['scenarios'])}组回放：对照、资金补位、高周期趋势回踩、1.5R保本、MA40两分钟确认。通过全部筛选：{'、'.join(passed) or '无'}。",
        f"研究保留方案为“{a['labels'][selected]}”：{before['trade_count']}→{after['trade_count']}笔；分窗净盈亏{before['net']:,.2f}→{after['net']:,.2f}元；最大单窗回撤{before['max_window_drawdown']:,.2f}→{after['max_window_drawdown']:,.2f}元。",
        f"保留方案在{after['active_days']}/{a['research_days']}个交易日有成交；剔除最大盈利单后{after['without_best_trade']:,.2f}元，剔除最大两笔后{after['without_two_best_trades']:,.2f}元。这些是已查看的开发窗口，不能据此确认样本外收益。",
    ]
    if a["selected_entry"] is None:
        notes.append("两项入场调整均未同时达到增单、增益和各窗口回撤不恶化。订单偏少的问题仍未解决，不能为了增加笔数采用已经显示收益或回撤恶化的版本。")
    else:
        c = a["changes"][a["selected_entry"]]
        notes.append(f"独立通过的入场方案新增{len(c['added'])}个不同入场时点，涉及{c['new_contract_day_directions']}组新增合约／日／方向，移除{len(c['removed'])}个原入场时点。同日改时点和重复入场仍需与新增机会区分。")
    for name in a["labels"]:
        if name == "control":
            continue
        t, p = a["totals"][name], a["promotion"][name]
        failed = [k for k, passed in p["checks"].items() if not passed]
        names = {"total_net_improves": "总净收益提高", "each_window_net_not_worse": "每窗净收益不恶化",
                 "each_window_drawdown_not_worse": "每窗回撤不恶化", "without_best_trade_net_not_worse": "去最大盈利单不恶化",
                 "minimum_trade_count": "入场方案至少增加一笔"}
        notes.append(f"{a['labels'][name]}：{t['trade_count']}笔，净盈亏{t['net']:,.2f}元，相对原版{p['total_net_delta']:+,.2f}元；{'通过' if p['passed'] else '未通过'}。" +
                     ("未通过项：" + "、".join(names[k] for k in failed) + "。" if failed else ""))
    notes.append(f"资金补位实际成交中，原排名第三及以后的交易有{len(selection['promoted_fills'])}笔；共涉及{selection['promoted_contract_day_directions']}组补位合约／日／方向。补位只跳过已确认最小开仓容量为零的合约；执行资料或ATR未知时保留排名名额，因此名额变化并不保证出现可成交信号。")
    lower = selection["lower_rank_observations"]
    notes.append(f"补位后的第三名及以后候选共观察{lower.get('observations', 0):,}个分钟，其中{lower.get('execution_available', 0):,}个具备可用执行资料，{lower.get('all_market_pass', 0)}个通过全部市场条件。被哪道门槛挡住的明细见selection_diagnosis.json；各项拒绝可能重叠。")
    for v in ("replacement", "trend"):
        c = a["changes"][v]
        notes.append(f"{a['labels'][v]}的收益归因：不同入场时点净盈亏{c['added_net']:,.2f}元，移除原入场净盈亏{c['removed_net']:,.2f}元，共同入场交易净变化{c['shared_net_delta']:+,.2f}元。不同入场时点包括同一天的提前、延后和再次入场。")
    largest = max((t for t in missed if t["original_net_pnl"] > 0), key=lambda t: t["original_net_pnl"], default=None)
    if largest:
        blockers = {"higher_trend_quality": "高周期质量未通过", "trend_window_valid": "资格窗口无效",
                    "pullback_after_armed": "没有资格取得后的MA10回踩及突破确认"}
        explanation = "、".join(blockers.get(k, k) for k in largest["market_rejections_at_original_signal"])
        notes.append(f"具体漏掉的盈利机会：原版{largest['original_entry'][:16].replace('T', ' ')} {largest['contract']}这笔净盈利{largest['original_net_pnl']:,.2f}元；同一信号时刻，回踩版因“{explanation or '持仓／触发状态变化'}”未进入。原时点的完整过滤记录已保留。这说明统一改成回踩会漏掉直接启动的趋势。")
    changed = max(a["changes"]["breakeven15"]["shared"], key=lambda t: abs(t["net_delta"]), default=None)
    if changed and abs(changed["net_delta"]) > 1e-6:
        before, after = changed["before"], changed["after"]
        notes.append(f"保本延后的具体影响：{before['entry_time'][:16].replace('T', ' ')} {before['contract']}同一入场，原方案{before['exit_time'][11:16]}退出，净盈亏{float(before['net_pnl']):,.2f}元；1.5R版{after['exit_time'][11:16]}退出，净盈亏{float(after['net_pnl']):,.2f}元，变化{changed['net_delta']:+,.2f}元。图中分别显示实际保本启动线和已有保护线，跳空退出按当时可成交开盘价核对。")
    notes.extend([
        "入场预期已明确区分：原版和资金补位版采用条件首次全部通过后的下一可交易开盘；趋势回踩版先用已完成的5分钟和15分钟取得资格，再在同一交易小节的五分钟有效期内等待MA10回踩及收盘突破前根高／低。回踩须在资格取得后完成，同一回踩只消费一次。",
        "退出独立比较：保本启动由1R延后至1.5R，或MA40反穿由一根改为连续两根相邻完成分钟确认。初始硬止损、有效保本／追踪线、放量走弱和时间退出照常生效，没有最短持仓等待。",
        "图表保留原四面板样式，标出真实模拟成交价、信号时点、实际有效保护线及对应版本门槛。趋势版的1分钟效率和斜率仅作观察，入场质量门槛实际作用于5分钟；图中5分钟效率只在该根完成后更新。",
        "下一轮增单优先研究两条独立入场通道：保留原有条件首次通过的突破入场，增加趋势回踩作为补充。同一分钟触发时优先原突破，仍需固定同合约去重、共享资金和保护规则，再检验新增成交是否带来正的净贡献。该双通道尚未回测，本轮不事后混合失败版本。",
        "候选补位还需在新的声明中比较执行资料齐全、最小手数可承担且成本比例可接受的候选池。当前补位只跳过资金容量为零，未解决后续候选本身不满足成本与市场条件的问题。单独增加资金或放宽市场门槛，都不能由本轮结果推断净收益会提高。",
        "资金、手续费、每边1跳滑点、成本/ATR上限和历史执行资料假设沿用原版。每窗100万元单独初始化；分窗净盈亏合计不是连续账户收益，不年化。9/24—30锁定测试未读取，默认与实盘策略未切换。",
    ])
    return notes


def main(reuse=False):
    a = json.loads((OUTPUT / "assessment.json").read_text())
    require(a["status"] == "completed" and a["combination_status"] != "pending", "全部合格阶段尚未完成")
    plan = json.loads(PLAN.read_text())
    budget = SpaceBudget(plan["budget"])
    budget.check(CHARTS, reserve=(60 if reuse else 140) * 1024 * 1024)
    spec = importlib.util.spec_from_file_location("opportunity_trade_review", HERE / "build_opportunity_trade_review.py")
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    if reuse:
        data = review.read(CHARTS / "review_data.json")
    else:
        data = review.collect(report={**a, "assessment": a,
                    "sample_note": "7、8月与9/14—23均为已查看的开发样本；锁定测试未读取。"},
                    labels=a["labels"], focus=a["selected"], diagnose=False)
    selection = selection_diagnosis(a)
    missed = missed_control_opportunities(a)
    previous_path = OUTPUT.parent / "frequency_followup/assessment.json"
    previous = json.loads(previous_path.read_text())
    previous_candidate = {"source": str(previous_path), "sha256": file_sha256(previous_path),
        "variant": previous["selected"], "label": previous["labels"][previous["selected"]],
        "totals": previous["totals"][previous["selected"]], "not_combined_or_replayed_in_this_round": True}
    notes = findings(a, selection, missed)
    prev_total = previous_candidate["totals"]
    notes.append(f"前轮独立通过的“{previous_candidate['label']}”结果仍保留：{prev_total['trade_count']}笔、净盈亏{prev_total['net']:,.2f}元；去最大盈利单后{prev_total['without_best_trade']:,.2f}元。它减少了一笔亏损重复入场，也未解决增单。本轮按冻结声明使用原8笔作共同对照，没有回放该旧候选与新规则的组合。")
    data.update(assessment={k: v for k, v in a.items() if k != "scenarios"},
                findings=notes, selected=a["selected"], selection_diagnosis=selection,
                missed_original_signals=missed,
                previous_research_candidate=previous_candidate,
                scenarios=[{k: s[k] for k in ("variant", "month", "funnel", "diagnostics", "metrics")} for s in a["scenarios"]])
    control = {(c["trade"]["contract"], c["trade"]["direction"], c["trade"]["entry_time"]): c["trade"]
               for c in data["charts"] if c["trade"]["variant"] == "control"}
    for c in data["charts"]:
        t = c["trade"]
        old = control.get((t["contract"], t["direction"], t["entry_time"]))
        same_day = next((v for (key, direction, time), v in control.items()
                         if key == t["contract"] and direction == t["direction"] and time[:10] == t["entry_time"][:10]), None)
        if t["variant"] == "control":
            t["comparison"] = "原方案实际成交"
        elif old:
            t["comparison"], t["paired_uid"] = "与原方案相同入场时点", old["uid"]
        elif same_day:
            t["comparison"], t["paired_uid"] = "原同日同方向机会中的不同入场时点", same_day["uid"]
        else:
            t["comparison"] = "新增合约／日／方向的入场"
    data.pop("diagnostics", None)
    data["verification"].pop("strategy_changed", None)
    data["verification"].update(research_variants_compared=list(a["labels"]), production_strategy_changed=False)
    write_json(CHARTS / "review_data.json", data, budget)
    write_json(CHARTS / "verification.json", data["verification"], budget)
    write_json(CHARTS / "selection_diagnosis.json", selection, budget)
    review.build(data, template_path=HERE / "opportunity_followup.html.template",
                 destination=CHARTS / "opportunity_followup_review.html")
    md = ["# 资金补位、回踩入场与离场优化复核", "", *[p + "\n" for p in data["findings"]],
          "|方案|完整交易|净盈亏/元|最大单窗回撤/元|去最大盈利单/元|筛选|",
          "|---|---:|---:|---:|---:|---|"]
    for v, t in a["totals"].items():
        status = "基准" if v == "control" else "通过" if a["promotion"][v]["passed"] else "未通过"
        md.append(f"|{a['labels'][v]}|{t['trade_count']}|{t['net']:,.2f}|{t['max_window_drawdown']:,.2f}|{t['without_best_trade']:,.2f}|{status}|")
    (CHARTS / "review_notes.md").write_text("\n".join(md) + "\n")
    budget.check(CHARTS)
    print(json.dumps({"selected": a["selected"], "charts": len(data["charts"]),
                      "report": str(CHARTS / "opportunity_followup_review.html")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse", action="store_true")
    main(parser.parse_args().reuse)
