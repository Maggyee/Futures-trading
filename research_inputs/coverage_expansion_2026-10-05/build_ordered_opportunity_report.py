"""Deliver the frozen opportunity experiments, including failures and original chart panels."""

import argparse
import importlib.util
import json
import os
from collections import Counter
from pathlib import Path

from research.coverage_audit import rows
from research.frequency_followup import require
from research.ordered_opportunity import PLAN
from research.reporting import write_json
from research.storage import SpaceBudget

HERE = Path(__file__).parent
OUTPUT = Path(json.loads(PLAN.read_text())["output"])
CHARTS = OUTPUT / "trade_review"


def missed_control_opportunities(a):
    control = [t for s in a["scenarios"] if s["variant"] == "control" for t in s["trades"]]
    trend = [t for s in a["scenarios"] if s["variant"] == "dual" for t in s["trades"]]
    filled = {(t["contract"], t["direction"], t["entry_time"]) for t in trend}
    needed = {(t["contract"], t["entry_signal_time"]): t for t in control
              if (t["contract"], t["direction"], t["entry_time"]) not in filled}
    result = []
    for s in a["scenarios"]:
        if s["variant"] != "dual":
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
                "higher_context": json.loads(row["snapshot"]).get("trend_entry", {"efficiency": None}),
                "actual_pullback": json.loads(row["pullback"]) if row["pullback"] else None})
    return result


def selection_diagnosis(a):
    checks, selected_unknown, changes = Counter(), Counter(), set()
    lower_rank = Counter()
    promoted_trades, examples = [], {}
    for s in a["scenarios"]:
        if s["variant"] != "pool":
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


def findings(a, selection):
    before, after = a["totals"]["control"], a["totals"][a["selected"]]
    passed = [a["labels"][v] for v, p in a["promotion"].items() if p["passed"]]
    notes = [
        f"按固定顺序完成{len(a['scenarios'])}组回放：已通过的止损后禁入对照、可成交候选池、原突破＋回踩双通道，最后独立核验共用1R保护。通过全部筛选：{'、'.join(passed) or '无'}。",
        f"研究保留方案为“{a['labels'][a['selected']]}”：{before['trade_count']}→{after['trade_count']}笔；分窗净盈亏{before['net']:,.2f}→{after['net']:,.2f}元；最大单窗回撤{before['max_window_drawdown']:,.2f}→{after['max_window_drawdown']:,.2f}元。",
        f"剔除最大盈利单后{after['without_best_trade']:,.2f}元，剔除最大两笔后{after['without_two_best_trades']:,.2f}元。保留方案在{after['active_days']}/{a['research_days']}个交易日有成交。版本间相同交易不是新增独立样本。",
    ]
    for name, p in a["promotion"].items():
        t, c = a["totals"][name], a["changes"][name]
        notes.append(f"{a['labels'][name]}：{t['trade_count']}笔，净盈亏{t['net']:,.2f}元，相对7笔对照{p['total_net_delta']:+,.2f}元；{'通过' if p['passed'] else '未通过'}。不同入场时点{len(c['added'])}笔，净贡献{c['added_net']:,.2f}元；移除对照入场{len(c['removed'])}笔，其原净盈亏{c['removed_net']:,.2f}元；共同入场净变化{c['shared_net_delta']:+,.2f}元。")
        deteriorated = [f"{w['month']}净收益变化{w['net_delta']:+,.2f}元、回撤变化{w['drawdown_delta']:+,.2f}元"
                        for w in p["windows"] if w["net_delta"] < -1e-6 or w["drawdown_delta"] > 1e-6]
        if deteriorated:
            notes.append(f"{a['labels'][name]}未满足分窗要求：{'；'.join(deteriorated)}。回撤变化为正表示风险恶化。")
    august_pool = next(s for s in a["scenarios"] if s["variant"] == "pool" and s["month"] == "2026-08")
    paths = {p["id"]: p for p in august_pool["paths"]}
    early_stops = [t for t in august_pool["trades"] if t["exit_reason"] == "fixed_stop"
                   and float(t["holding_minutes"]) <= 3
                   and not paths[int(t["id"])]["breakeven_activated"]]
    if early_stops:
        notes.append(f"8月候选池的{len(early_stops)}/{len(august_pool['trades'])}笔交易在入场后不超过3个有效交易分钟触发初始止损，合计净盈亏{sum(float(t['net_pnl']) for t in early_stops):,.2f}元；这些交易均未启动1R保本。下一步应检验入场后的趋势延续与短暂冲高回落如何区分，延后保本启动不会改变这些原有止损事件。该方向尚未回测，不作为已通过改进。")
    counts = selection["checks"]
    lower = selection["lower_rank_observations"]
    notes.append(f"候选池逐分钟检查中，因执行资料不可用跳过{counts.get('skip_unavailable', 0):,}次，因资金容量为零跳过{counts.get('skip_zero_capacity', 0):,}次，因成本比例超限跳过{counts.get('skip_cost', 0):,}次。原因按固定顺序记录，这些是重复分钟检查，不是独立机会。")
    notes.append(f"第三名及以后的实际候选共观察{lower.get('observations', 0):,}个分钟；其中{lower.get('all_market_pass', 0)}个通过全部市场条件，产生{len(selection['promoted_fills'])}笔实际交易。候选排名仍为原R8顺序；资金、最小手数与成本筛选均使用当时已知数据。")
    dual = [s for s in a["scenarios"] if s["variant"] == "dual"]
    trades = [t for s in dual for t in s["trades"]]
    pullbacks = [t for t in trades if json.loads(t["entry_snapshot"]).get("entry_channel") == "pullback"]
    notes.append(f"双通道实际成交中，突破通道{len(trades)-len(pullbacks)}笔，回踩通道{len(pullbacks)}笔；回踩成交净盈亏合计{sum(float(t['net_pnl']) for t in pullbacks):,.2f}元。回踩通道的合计不等同于新增净贡献，须同时考虑被替换或受资金占用影响的原交易。")
    lost = a["changes"]["dual"]["removed"]
    notes.append(f"双通道未保留的对照入场时点有{len(lost)}个；原交易中盈利的有{sum(float(t['net_pnl']) > 0 for t in lost)}笔。逐笔变化和相同时刻状态均保留，可在报告中跳转核对。")
    august_dual = next(s for s in dual if s["month"] == "2026-08")["diagnostics"]
    notes.append(f"8月双通道产生{august_dual['entry_trigger']}次入场触发，其中{august_dual['risk_rejected_triggers']}次被资金预算拒绝、{august_dual['fill_cancellation_count']}次在成交前撤单，实际成交{august_dual['actual_fill']}笔。有效信号还需要通过资金与成交价格检查。")
    for c in august_dual["fill_cancellations"]:
        if c["reason"] == "fill_price_recheck":
            notes.append(f"{c['contract']}在{c['time'][:16].replace('T', ' ')}的含滑点模拟成交价为{float(c['modeled_price']):,.0f}，原允许成交边界为{float(c['modeled_price_limit']):,.0f}；开盘复核未通过而撤单。可另行检验保留原价格边界与短有效期的有限重试；目前没有重试回测结果，不能认定其能恢复交易或提高净收益。")
    if a["selected_entry"] is None:
        notes.append("本轮没有找到同时增单、增益且满足各窗口回撤要求的版本。失败方案不替换已通过的研究候选；订单偏少仍需进一步研究。新增成交如果只增加亏损，也不能作为改进。")
    notes.extend([
        "同合约两通道同时触发时只提交一个突破请求；同一分钟先给突破请求分配资金，再按原排名排序。同合约已有持仓或预占订单时不重复开仓。回踩只在已完成5/15分钟趋势合格的五分钟窗口内触发，须有完成的MA10回踩及前根高低点突破；窗口不跨交易小节。两通道共用资金、冷却和同日同方向止损后禁入规则。",
        "保本保留1R并覆盖交易成本；初始硬止损、原目标启动后的2ATR追踪、单根MA40确认、放量走弱和时间退出均逐笔核验。已有保护线触及时不等待下一次趋势确认；休市后的跳空按实际可成交开盘价计算。此前失败的1.5R保本和MA40两根确认不在本轮重复调参。",
        "图中按实际成交通道显示门槛：突破使用原1分钟效率与1/5分钟斜率；回踩使用5分钟趋势质量，1分钟斜率仅供观察。四面板保留真实分钟K线、均线、近似均价、量比、持仓量及保护线。",
        "每窗100万元独立初始化，费用、每边1跳滑点、风险上限、原合约池和排名均沿用对照。三个窗口净盈亏合计不是连续账户收益，不年化。仅使用7、8月及9/14—23的52个开发交易日；9/24—30锁定测试未读取，默认与实盘未切换。",
    ])
    return notes


def main(reuse=False):
    os.umask(0o077)
    a = json.loads((OUTPUT / "assessment.json").read_text())
    require(a["status"] == "completed", "顺序回测尚未完成")
    plan = json.loads(PLAN.read_text())
    budget = SpaceBudget(plan["budget"])
    # Collection is in memory; each artifact writer checks its measured bytes.
    budget.check(CHARTS)
    spec = importlib.util.spec_from_file_location("ordered_trade_review", HERE / "build_ordered_opportunity_trade_review.py")
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    if reuse:
        data = review.read(CHARTS / "review_data.json")
    else:
        data = review.collect(report={**a, "assessment": a,
            "sample_note": "7、8月与9/14—23均为已查看开发样本，锁定测试未读取。"},
            labels=a["labels"], focus=a["selected"], diagnose=False)
    selection, missed = selection_diagnosis(a), missed_control_opportunities(a)
    notes = findings(a, selection)
    data.update(assessment={k: v for k, v in a.items() if k != "scenarios"},
                findings=notes, selected=a["selected"], selection_diagnosis=selection,
                missed_original_signals=missed,
                scenarios=[{k: s[k] for k in ("variant", "month", "funnel", "diagnostics", "metrics")} for s in a["scenarios"]])
    control = {(c["trade"]["contract"], c["trade"]["direction"], c["trade"]["entry_time"]): c["trade"]
               for c in data["charts"] if c["trade"]["variant"] == "control"}
    for c in data["charts"]:
        t = c["trade"]
        old = control.get((t["contract"], t["direction"], t["entry_time"]))
        same_day = next((v for (key, direction, time), v in control.items()
                         if key == t["contract"] and direction == t["direction"] and time[:10] == t["entry_time"][:10]), None)
        if t["variant"] == "control":
            t["comparison"] = "已通过的止损后禁入对照"
        elif old:
            t["comparison"], t["paired_uid"] = "与7笔对照相同入场时点", old["uid"]
        elif same_day:
            t["comparison"], t["paired_uid"] = "同日同方向机会中的不同入场时点", same_day["uid"]
        else:
            t["comparison"] = "新增合约／日／方向的入场"
    data.pop("diagnostics", None)
    data["verification"].pop("strategy_changed", None)
    data["verification"].update(research_variants_compared=list(a["labels"]), production_strategy_changed=False)
    review.dump(CHARTS / "review_data.json", data)
    write_json(CHARTS / "verification.json", data["verification"], budget)
    write_json(CHARTS / "selection_diagnosis.json", selection, budget)
    write_json(CHARTS / "assessment.json", a, budget)
    review.build(data, template_path=HERE / "ordered_opportunity.html.template",
                 destination=CHARTS / "ordered_opportunity_review.html")
    md = ["# 可成交候选池、双通道与1R保护顺序优化", "", *[p + "\n" for p in notes],
          "|方案|完整交易|净盈亏/元|最大单窗回撤/元|去最大盈利单/元|筛选|",
          "|---|---:|---:|---:|---:|---|"]
    for name, t in a["totals"].items():
        status = "对照" if name == "control" else "通过" if a["promotion"][name]["passed"] else "未通过"
        md.append(f"|{a['labels'][name]}|{t['trade_count']}|{t['net']:,.2f}|{t['max_window_drawdown']:,.2f}|{t['without_best_trade']:,.2f}|{status}|")
    content = ("\n".join(md) + "\n").encode()
    budget.check(CHARTS / "review_notes.md", reserve=len(content))
    (CHARTS / "review_notes.md").write_bytes(content)
    budget.check(CHARTS)
    print(json.dumps({"selected": a["selected"], "charts": len(data["charts"]),
                      "report": str(CHARTS / "ordered_opportunity_review.html")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse", action="store_true")
    main(parser.parse_args().reuse)
