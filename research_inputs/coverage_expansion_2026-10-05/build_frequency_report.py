"""Publish audited follow-up results with the original entry/exit chart panels."""

import argparse
import importlib.util
import json
from pathlib import Path

from research.frequency_followup import PLAN, require
from research.storage import SpaceBudget

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
OUTPUT = ROOT / "research_outputs/coverage_expansion_2026-10-05/frequency_followup"
CHARTS = OUTPUT / "trade_review"


def module():
    spec = importlib.util.spec_from_file_location(
        "previous_trade_review", HERE / "build_latest_trade_review.py"
    )
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    review.OUT = CHARTS
    return review


def findings(a):
    selected = a["selected"]
    before, after = a["totals"]["control"], a["totals"][selected]
    passed = [a["labels"][k] for k, v in a["promotion"].items() if v["passed"]]
    notes = [
        f"本轮完成{len(a['scenarios'])}组串行回放，固定四项独立改动，并按事前条件决定是否组合。通过全部筛选条件：{'、'.join(passed) or '无'}。",
        f"后续研究候选为“{a['labels'][selected]}”：{before['trade_count']}→{after['trade_count']}笔，分窗净盈亏合计{before['net']:,.2f}→{after['net']:,.2f}元，最大单窗回撤{before['max_window_drawdown']:,.2f}→{after['max_window_drawdown']:,.2f}元。",
        f"候选共有{after['active_days']}/{a['research_days']}天发生交易；剔除最大盈利单后{after['without_best_trade']:,.2f}元，剔除最大两笔后{after['without_two_best_trades']:,.2f}元。即使通过本轮筛选，也不能由此认定收益稳定。",
    ]
    if a["selected_entry"] is None:
        notes.append(
            "本轮没有入场放宽方案同时满足：增加交易、总净收益改善、每个窗口收益和回撤不恶化，以及剔除最大盈利单后不恶化。订单太少的问题仍未解决；不继续根据本轮结果反复调同一门槛。"
        )
    else:
        change = a["changes"][selected]
        notes.append(
            f"候选相对对照出现{len(change['added'])}个不同的成交入场时点，其中涉及{change['new_contract_day_directions']}组新增合约／交易日／方向；移除{len(change['removed'])}笔原入场。同日改时点和重复再入场不能视为新的独立样本。"
        )
    for name in ["efficiency", "slope5", "oi", "stop_reentry"]:
        t = a["totals"][name]
        d = a["promotion"][name]
        notes.append(
            f"{a['labels'][name]}：{t['trade_count']}笔，净盈亏{t['net']:,.2f}元；{'通过' if d['passed'] else '未通过'}预声明筛选。相对对照收益变化{d['total_net_delta']:+,.2f}元。"
        )
    blocked = a["changes"]["stop_reentry"]
    oi_runs = [s for s in a["scenarios"] if s["variant"] == "oi"]
    triggers = sum(s["diagnostics"]["entry_trigger"] for s in oi_runs)
    rejected = sum(s["diagnostics"].get("risk_rejected_triggers", 0) for s in oi_runs)
    notes.append(
        f"关闭增仓后共有{triggers}次触发，其中{rejected}次被资金限制拒绝；连续重试不算独立机会。增加合格信号不等于增加可成交交易，金融组保证金和最小手数风险仍是约束。"
    )
    if not blocked["added"] and abs(blocked["shared_net_delta"]) < 1e-7:
        details = "；".join(
            f"{t['entry_time'][:16].replace('T', ' ')} {t['contract']}，原净盈亏{float(t['net_pnl']):,.2f}元"
            for t in blocked["removed"]
        )
        notes.append(
            f"止损后禁入的收益变化全部来自移除{len(blocked['removed'])}笔原交易，其余共同交易的净盈亏完全相同：{details or '没有移除交易'}。这说明本轮改善集中于少数重复入场案例，尚不足以证明该限制在新样本中持续有效。"
        )
    cancellations = [
        c
        for s in a["scenarios"]
        if s["variant"] == "efficiency"
        for c in s["diagnostics"]["fill_cancellations"]
    ]
    example = next(
        (
            c
            for c in cancellations
            if c["contract"] == "lc2609.GFEX"
            and c["time"] == "2026-07-27T14:05:00+08:00"
        ),
        None,
    )
    if example:
        same_run = next(
            s
            for s in a["scenarios"]
            if s["variant"] == "efficiency" and s["month"] == "2026-07"
        )
        silent = {
            (r["contract"], r["time"])
            for r in same_run["diagnostics"]["qualified_without_new_trigger"]
        }
        require(
            all(
                ("lc2609.GFEX", f"2026-07-27T14:0{i}:00+08:00") in silent
                for i in [6, 7, 8]
            ),
            "撤单后持续通过的例子不符",
        )
        notes.append(
            f"具体触发变化：效率0.35在7/27 14:05提前发出LC多头请求，含滑点成交价{float(example['modeled_price']):,.0f}超过当时允许上限{float(example['modeled_price_limit']):,.0f}而撤单；14:06—14:08条件持续通过，direct没有再次触发。原版14:06成交的亏损单因此消失。这是首次触发与成交边界共同造成的结果，不能解释为趋势判断本身变准。"
        )
        baseline_cancels = sum(
            s["diagnostics"]["fill_cancellation_count"]
            for s in a["scenarios"]
            if s["variant"] == "control"
        )
        notes.append(
            f"上一轮组合版在三个窗口共有{baseline_cancels}次成交前撤单；撤单后的触发机制解释了实验间差异，但不是原版交易稀少的主要来源。"
        )
    notes.extend(
        [
            "逐笔图已核对全部入场门槛、成交价、开平事件和有效保护线。direct入场是条件首次全部满足后，在下一可成交分钟开盘执行，仍不强制回踩MA10，也不保证局部最低／最高点。",
            "退出继续采用初始止损、1R后含成本保本、到目标后启动2ATR追踪，以及MA40／放量走弱／时间退出；没有最短持仓要求。止损后禁入只在实际亏损的初始止损成交后生效，限制当日同合约同方向，次交易日恢复。",
            "下一步优先将高周期趋势资格与低周期触发拆开设计，明确首次通过或回踩后触发的预期；若研究撤单后的有限重试，应保留首次信号价格边界并事前规定有效期。维持成本、风险与执行资料门槛，以有限版本和新的独立窗口验证。针对短持仓和盈利回吐，单独比较退出规则，避免事后按单挪动离场点。",
        ]
    )
    return notes


def main(reuse=False):
    a = json.loads((OUTPUT / "assessment.json").read_text())
    require(a["combination_status"] != "pending", "有条件组合尚未完成")
    plan = json.loads(PLAN.read_text())
    budget = SpaceBudget(plan["budget"])
    # Reuse only rewrites the saved chart data and report; it collects no new data.
    budget.check(CHARTS, reserve=(60 if reuse else 180) * 1024 * 1024)
    review = module()
    if reuse:
        data = review.read(CHARTS / "review_data.json")
    else:
        report = {
            **a,
            "assessment": a,
            "sample_note": "7、8月为9月参数冻结后的历史回放；9/14—23为已反复查看的开发样本。",
        }
        data = review.collect(
            report=report, labels=a["labels"], focus=a["selected"], diagnose=False
        )
    data.update(
        assessment={k: v for k, v in a.items() if k != "scenarios"},
        findings=findings(a),
        selected=a["selected"],
        scenarios=[
            {k: s[k] for k in ["variant", "month", "funnel", "diagnostics", "metrics"]}
            for s in a["scenarios"]
        ],
    )
    control = {
        (c["trade"]["contract"], c["trade"]["direction"], c["trade"]["entry_time"]): c[
            "trade"
        ]
        for c in data["charts"]
        if c["trade"]["variant"] == "control"
    }
    for c in data["charts"]:
        t = c["trade"]
        old = control.get((t["contract"], t["direction"], t["entry_time"]))
        if t["variant"] == "control":
            t["comparison"] = "上一轮实际成交"
        elif old:
            t["comparison"] = "与上一轮相同入场时点"
            t["paired_uid"] = old["uid"]
        else:
            same_day = next(
                (
                    v
                    for (key, direction, time), v in control.items()
                    if key == t["contract"]
                    and direction == t["direction"]
                    and time[:10] == t["entry_time"][:10]
                ),
                None,
            )
            t["comparison"] = (
                "原同日同方向机会中的不同入场时点"
                if same_day
                else "新增合约／日／方向的入场"
            )
            if same_day:
                t["paired_uid"] = same_day["uid"]
        if t["variant"] == "stop_reentry":
            removed = next(
                (
                    r
                    for r in a["changes"]["stop_reentry"]["removed"]
                    if r["contract"] == t["contract"]
                    and r["direction"] == t["direction"]
                    and r["entry_time"][:10] == t["entry_time"][:10]
                    and r["entry_time"] > t["exit_time"]
                ),
                None,
            )
            if removed:
                t["blocked_reentry_uid"] = control[
                    (removed["contract"], removed["direction"], removed["entry_time"])
                ]["uid"]
    data.pop("diagnostics", None)
    data["verification"].pop("strategy_changed", None)
    data["verification"]["research_variants_compared"] = list(a["labels"])
    data["verification"]["production_strategy_changed"] = False
    review.dump(CHARTS / "review_data.json", data)
    review.dump(CHARTS / "verification.json", data["verification"])
    review.build(
        data,
        template_path=HERE / "frequency_followup.html.template",
        destination=CHARTS / "frequency_followup_review.html",
    )
    md = [
        "# 交易频率优化回测复核",
        "",
        *[p + "\n" for p in data["findings"]],
        "## 对照结果",
        "",
        "|方案|完整交易|净盈亏/元|最大单窗回撤/元|剔除最大盈利单/元|筛选|",
        "|---|---:|---:|---:|---:|---|",
    ]
    for v, t in a["totals"].items():
        status = (
            "基准"
            if v == "control"
            else "通过"
            if a["promotion"][v]["passed"]
            else "未通过"
        )
        md.append(
            f"|{a['labels'][v]}|{t['trade_count']}|{t['net']:,.2f}|{t['max_window_drawdown']:,.2f}|{t['without_best_trade']:,.2f}|{status}|"
        )
    md.extend(
        [
            "",
            "每个窗口独立以100万元初始化，分窗净盈亏合计不是连续账户收益，不年化。费用已扣、每边1跳滑点已计入成交价；历史执行资料及期货公司费用假设沿用上一轮。9/24—30锁定测试未读取，不能当作样本外验证。",
            "",
            "所有实验和失败筛选均保留；默认和实盘策略未切换。完整进出场、参数和失败条件见同目录HTML，逐笔成交见trade_review.csv，数据指纹见verification.json。",
            "",
        ]
    )
    (CHARTS / "review_notes.md").write_text("\n".join(md))
    budget.check(CHARTS)
    print(
        json.dumps(
            {
                "selected": a["selected"],
                "charts": len(data["charts"]),
                "report": str(CHARTS / "frequency_followup_review.html"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse", action="store_true")
    main(parser.parse_args().reuse)
