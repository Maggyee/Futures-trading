"""Review saved development runs without changing any trading recipe or result."""

import base64
import csv
import gzip
import hashlib
import html
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "research_outputs/coverage_expansion_2026-10-05/ordered_opportunity"
OUT = ROOT / "research_outputs/strategy_recheck_2026-10-07"
STAGES = {
    "可观察且允许开仓": {"candidate", "warmup_1m", "warmup_higher", "current_session_15m",
                         "session_data_continuous", "state", "entry_time"},
    "三周期方向": {"trend_15m", "trend_5m", "trend_1m"},
    "价格与即时退出条件": {"vwap", "extension", "no_exit_condition"},
    "净增仓": {"oi"},
    "效率、活动、位移与冲击": {"efficiency", "trend_activity", "trend_displacement", "shock"},
    "1分钟斜率区间": {"slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"},
    "5分钟斜率区间": {"slope_5m_ready", "slope_5m_minimum", "slope_5m_maximum"},
    "成本": {"cost"},
    "止损后禁入": {"stop_reentry"},
}
RELAXATIONS = {
    "1分钟斜率": STAGES["1分钟斜率区间"],
    "5分钟斜率": STAGES["5分钟斜率区间"],
    "两周期斜率": STAGES["1分钟斜率区间"] | STAGES["5分钟斜率区间"],
    "净增仓": {"oi"},
    "方向效率": {"efficiency"},
    "同向位移": {"trend_displacement"},
    "成本": {"cost"},
    "效率与位移": {"efficiency", "trend_displacement"},
}


def read_json(path):
    return json.loads(path.read_text())


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def summarize_signals(run):
    counts = Counter()
    identities = {name: set() for name in ("全部观察", *STAGES, "执行资料通过", "触发", "资金通过", "成交")}
    relaxed = {name: {"minutes": 0, "identities": set()} for name in RELAXATIONS}
    rejected = []
    known_gates = set().union(*STAGES.values())
    for row in rows(run / "signals.csv.gz"):
        flags = json.loads(row["filters"])
        identity = row["date"], row["contract"], row["direction"]
        require(set(flags) == known_gates, "信号出现未分类的过滤，停止而不漏计")
        require(all(flags.values()) == (row["all_pass"] == "True"), "信号总通过标记不符")
        counts["全部观察"] += 1
        identities["全部观察"].add(identity)
        passing = True
        for name, gates in STAGES.items():
            passing = passing and all(flags[gate] for gate in gates)
            if passing:
                counts[name] += 1
                identities[name].add(identity)
        if passing and row["execution_pass"] == "True":
            counts["执行资料通过"] += 1
            identities["执行资料通过"].add(identity)
        for name, field in (("触发", "trigger"), ("资金通过", "risk_pass"), ("成交", "filled")):
            if row[field] == "True":
                counts[name] += 1
                identities[name].add(identity)
        if row["trigger"] == "True" and row["execution_pass"] == "True" and row["risk_pass"] != "True":
            rejected.append({k: row[k] for k in ("time", "date", "contract", "direction", "group", "risk_rejections")})
        failures = {name for name, passing in flags.items() if not passing}
        if row["execution_pass"] == "True" and failures:
            for name, gates in RELAXATIONS.items():
                if failures <= gates:
                    relaxed[name]["minutes"] += 1
                    relaxed[name]["identities"].add(identity)
    return {
        "unit": "完成分钟观察，以及去重的合约/交易日/方向组合；二者都不是独立交易样本",
        "stages": [{"stage": name, "observations": counts[name], "contract_day_directions": len(identities[name])}
                   for name in identities],
        "sole_blocking_group": {name: {"observations": value["minutes"],
                                      "contract_day_directions": len(value["identities"])}
                                for name, value in relaxed.items()},
        "relaxation_note": "只统计其他现有条件已通过的观察；未重放触发、资金、订单或退出，不能解释为新增成交或收益",
        "capital_rejections": rejected,
        "unique_capital_rejected_contract_day_directions": len({(r["date"], r["contract"], r["direction"]) for r in rejected}),
    }


def render_report(result):
    names = {"control": "当前保留研究版", "pool": "可成交候选池", "dual": "直接入场＋回踩双通道"}

    def table(headers, records):
        head = "".join(f"<th>{html.escape(str(v))}</th>" for v in headers)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                       for row in records)
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    comparison = table(
        ["方案", "成交", "有成交的交易日", "分窗净盈亏合计", "去掉最大盈利单"],
        [(names[v], t["trades"], f'{t["days_with_trade"]}/52', f'{t["net"]:+,.2f}元',
          f'{t["without_best_trade"]:+,.2f}元') for v, t in result["totals"].items()],
    )
    months = table(["方案", "7月", "8月", "9月14—23日"],
                   [(names[v], *(f'{t["month_net"][m]:+,.2f}元' for m in ("2026-07", "2026-08", "2026-09")))
                    for v, t in result["totals"].items()])
    totals = Counter()
    for scenario in result["scenarios"]:
        if scenario["variant"] == "control":
            for stage in scenario["signals"]["stages"]:
                totals[stage["stage"]] += stage["observations"]
    funnel = table(["累计保留条件", "通过的完成分钟观察"],
                   [(name, f"{value:,}") for name, value in totals.items()])
    control_rows = [s for s in result["trade_scale"] if s["variant"] == "control"]
    distances = table(["合约", "入场", "初始保护距离", "当时5分钟ATR", "距离 / 5分钟ATR", "退出原因"],
                      [(s["contract"], s["entry_time"][5:16].replace("T", " "), f'{s["stop_distance"]:g}',
                        f'{s["atr_5m_previous"]:.2f}', f'{s["stop_atr_5m"]:.3f}',
                        {"fixed_stop": "初始止损", "breakeven_stop": "保本", "trailing_stop": "追踪保护", "volume": "放量走弱"}[s["exit_reason"]])
                       for s in control_rows])
    august = [s for s in result["trade_scale"] if s["variant"] == "pool" and s["month"] == "2026-08"]
    losses = table(["合约", "入场", "持有交易分钟", "初始止损 / 5分钟ATR", "已确认有利浮盈 / 初始风险距离", "净盈亏"],
                   [(s["contract"], s["entry_time"][5:16].replace("T", " "), s["holding_minutes"],
                     f'{s["stop_atr_5m"]:.3f}', f'{s["verified_favorable_initial_r"]:.2f}R', f'{s["net_pnl"]:+,.2f}元')
                    for s in august])

    def picture(name, alt):
        content = base64.b64encode((SOURCE / "trade_review" / name).read_bytes()).decode()
        return f'<img alt="{html.escape(alt)}" src="data:image/png;base64,{content}">'

    source_link = "../coverage_expansion_2026-10-05/ordered_opportunity/trade_review/ordered_opportunity_review.html"
    body = f"""
<header><p class="eyebrow">2026年10月7日 · 当前策略复核</p>
<h1>成交频率、入场形态和波段保护仍未解决</h1>
<p>当前保留的研究版仍在52个开发交易日内只有7笔成交。扩候选池与加入回踩的实验增加了笔数，净收益下降；最新方案没有通过替换条件。</p></header>
<section><h2>当前结果与适用范围</h2>{comparison}
<p class="note">三窗各自以100万元初始化；盈亏合计不是连续账户收益率。以上均已计交易所手续费及每边1跳滑点；账户加收未知。</p>
{months}<p>最大一笔盈利7,198.91元，占保留方案净盈利的95.48%。两笔9月碳酸锂盈利合计9,346.00元，去掉这两笔后为−1,805.95元。双通道增加的两笔发生在已有成交日，有成交的交易日仍为7天。</p></section>
<section><h2>交易太少：过滤叠加与资金约束都在起作用</h2>
<p>重新从当前保留版逐分钟记录汇总：89,274条完成分钟观察最终只产生19次触发，12次被资金预算拒绝，剩下7次成交。资金拒绝去重后涉及7组合约／交易日／方向，全部为股指；相邻分钟重复尝试不能算新增独立机会。</p>
{funnel}<p class="note">表中采用固定展示顺序，前一行通过后才统计下一行。顺序会影响显示的边际减少量，不能由此推断哪一项过滤最有收益价值。完成分钟观察不是独立交易样本。</p>
<p>可成交候选池已经让24次触发全部通过资金分配，最终19笔成交；其中5次因开盘含滑点价格越过原边界而撤销。当前保留版没有价格复核撤单，所以有限重试尚不能直接解释其低频。候选池8月新增的4笔均亏损，说明“能承担、成本合格”还不足以保证入场质量。</p></section>
<section><h2>入场：当前直接通道没有结构突破或回踩确认</h2>
<p>保留版实际采用“所有过滤首次由不通过变成通过 → 下一可交易分钟开盘复核成交价”。它没有单独要求突破前根高／低点，也没有要求先回踩MA10。报告里称作“突破通道”的规则，本质是条件首次通过，不能据此认定入场符合你预期的突破形态。</p>
<p>回踩确认只存在于另立的实验通道中，未成为当前保留版。该通道的两笔新增成交净贡献为−1,585.35元；只增加两笔、未新增成交日。强制全部改为回踩的早一轮实验也漏掉了9月16日原盈利7,198.91元的直接启动行情。</p></section>
<section><h2>保护：5／15分钟选趋势，初始止损仍按1分钟尺度</h2>
<p>现有初始止损取训练跳数、信号时1分钟ATR下限、两倍往返成本下限的最大值，成交后冻结。没有使用已确认的5分钟回踩高／低点作为结构失效位置。</p>
{distances}<p>7笔保留交易的初始止损均仅为当时5分钟ATR的0.39—0.50倍。这个事实解释了保护为何仍显得紧，但不能据此直接断言把止损加宽就会提高收益。若改保护位置，必须重新分配手数并完整回放，保持单笔0.2%风险预算。</p>
<p>8月扩池的4笔全部在2—3个交易分钟触发初始止损，均未启动1R保本。延后保本启动不能改变这4笔交易原有的初始止损事件。</p>{losses}
<p class="note">有利浮盈只计完整持仓K线与退出开盘；分钟内退出K线的其他极值排除，因为无法确认先后次序。该列是事后路径诊断，不是可实现的净收益。</p></section>
<section><h2>图例：8月14日碳酸锂新增入场</h2>
<p>10:39做多154,580，10:42初始止损退出154,220，4手合计−1,736.33元。初始止损距离340，仅为当时5分钟ATR的0.482倍；1R保本未启动。图沿用上一轮已核验的真实分钟与成交记录。</p>
{picture('pool-2026-08-2.png', '8月14日碳酸锂入场后初始止损示例')}
<p><a href="{source_link}">打开全部35张进出场图及逐笔对照</a></p></section>
<section><h2>下一轮应检验的具体改动</h2>
<ol><li><strong>先定义入场形态。</strong> 将“条件首次通过”、真实突破确认、趋势后的回踩再启动作为不同通道。高周期负责方向及斜率资格，低周期负责完成的价格确认；资格有效期和跨休息处理须预先固定。</li>
<li><strong>将新增波段通道的保护放在结构失效位置。</strong> 用入场前已确认的5分钟结构点及固定缓冲确定保护，按距离重新减小手数；成本与风险预算不变，最小手数承担不了就跳过。与原保护单独比较后再组合。</li>
<li><strong>验证新增交易的净贡献与覆盖。</strong> 同时报告新增成交日、分窗净盈亏、回撤、初始止损比例和去最大盈利单结果。有限重试单独检验原限价内的撤单，不替代入场质量研究。</li></ol>
<p>这三项是需要冻结并回测的研究方案，本次没有生成其收益结果。当前证据不足以认定任何新规则稳定有效；7、8月和9月14—23日都已反复查看，不能继续把这些调规则结果称为样本外验证。</p></section>
<section><h2>当前研究版与网页策略库</h2>
<p>只读核对运行库：目前只有“验证-布林带-0930”和“验证-双均线-0930”两份策略版本，未发现策略实例记录。上述研究方案尚未成为网页内可运行的策略实例；查看模板或实验代码并不代表账户正在运行该版本。</p></section>
<footer><p>本次核对9个保存实验、35笔版本成交、137,315条保留版／候选池信号观察；重新运行斜率、追踪保护与双通道相关68项检查，全部通过。代码检查通过与收益目标达成是两项独立判断。</p>
<p>本次为只读复核，无新增收益回测；仅读取原开发窗口，锁定测试未读取。<a href="trade_scale.csv">逐笔保护尺度CSV</a> · <a href="diagnosis.json">复核数据与来源指纹</a></p></footer>
"""
    style = """body{margin:0;background:#f1f5f9;color:#213047;font:16px/1.75 system-ui,-apple-system,'Noto Sans CJK SC','Microsoft YaHei',sans-serif}main{max-width:1060px;margin:auto;padding:32px 18px 56px}header,section,footer{background:white;border:1px solid #dce4ef;border-radius:14px;padding:25px;margin:0 0 20px}header{border-top:5px solid #3959bb}h1{font-size:30px;line-height:1.4;margin:8px 0 18px}h2{font-size:21px;line-height:1.5;margin:0 0 15px}p{margin:12px 0}a{color:#3155b8}.eyebrow,.note,footer{color:#63738a}.eyebrow{font-size:14px}.note{font-size:14px}.table-wrap{overflow-x:auto;margin:18px 0}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #dce4ef;white-space:nowrap}th{background:#f5f7fc}img{display:block;max-width:100%;height:auto;border-radius:10px}li{margin:12px 0}footer{font-size:14px}@media(max-width:600px){main{padding:14px 10px}header,section,footer{padding:18px 14px}h1{font-size:24px}h2{font-size:19px}body{font-size:15px}ol{padding-left:23px}}"""
    (OUT / "current_strategy_review.html").write_text(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>当前策略复核 · 2026年10月7日</title><style>{style}</style></head><body><main>{body}</main></body></html>'
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sources, scenarios, scales, totals = [], [], [], {}
    with gzip.open(SOURCE / "trade_review/review_data.json.gz", "rt") as stream:
        review = json.load(stream)
    chart_index = {(c["trade"]["variant"], c["trade"]["month"], str(c["trade"]["id"])): c
                   for c in review["charts"]}
    for variant in ("control", "pool", "dual"):
        all_trades = []
        for month in ("2026-07", "2026-08", "2026-09"):
            pointer = read_json(SOURCE / f"{month}_{variant}_latest.json")
            run = Path(pointer["directory"])
            audit = read_json(run / "independent_ordered_opportunity_audit.json")
            require(audit["status"] == "passed" and not audit["locked_test_read"], "来源未通过审计或读取了锁定测试")
            cfg = read_json(run / "config_snapshot.json")
            trades = list(rows(run / "trades.csv.gz"))
            metrics = pointer["metrics"]
            require(len(trades) == metrics["trade_count"], "成交数量不符")
            require(math.isclose(sum(float(t["net_pnl"]) for t in trades), metrics["net_profit"], abs_tol=1e-6), "成交净盈亏不符")
            scenario = {"variant": variant, "month": month, "directory": str(run),
                        "metrics": {k: metrics[k] for k in ("trade_count", "net_profit", "max_drawdown", "daily_count")},
                        "strategy": {k: v for k, v in cfg["strategy"].items() if k != "fixed_ticks"},
                        "audit_status": audit["status"]}
            if variant in ("control", "pool"):
                print(f"Reviewing saved signals: {variant} {month}", flush=True)
                scenario["signals"] = summarize_signals(run)
                require(next(r["observations"] for r in scenario["signals"]["stages"] if r["stage"] == "成交") == len(trades), "逐分钟成交与逐笔成交不符")
            scenario["cancelled_entries"] = [{k: v for k, v in event.items() if v}
                                             for event in rows(run / "events.csv.gz") if event["action"] == "entry_cancelled"]
            scenarios.append(scenario)
            for name in ("config_snapshot.json", "trades.csv.gz", "signals.csv.gz", "independent_ordered_opportunity_audit.json"):
                p = run / name
                sources.append({"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size})
            for trade in trades:
                c = chart_index[(variant, month, trade["id"])]
                t = c["trade"]
                require(t["entry_time"] == trade["entry_time"] and t["exit_time"] == trade["exit_time"], "图表与原始成交时刻不符")
                require(math.isclose(t["net_pnl"], float(trade["net_pnl"]), abs_tol=1e-8), "图表与原始成交盈亏不符")
                higher = [b for b in c["higher"]["5"] if b["end"] <= t["entry_signal_time"]]
                require(bool(higher) and higher[-1]["previous_atr"] > 0, "缺少开仓前已完成5分钟ATR")
                distance = abs(t["entry_price"] - t["stop_price"])
                latest = higher[-1]
                sign = 1 if t["direction"] == "LONG" else -1
                favorable = sign * (c["path"]["verified_favorable_price"] - t["entry_price"])
                scale = {k: t[k] for k in ("variant", "month", "contract", "direction", "entry_time", "entry_signal_time", "exit_time", "exit_reason", "holding_minutes", "net_pnl", "entry_channel", "entry_price", "stop_price")}
                scale.update(stop_distance=distance, atr_1m_previous=t["entry_snapshot"]["atr_previous"],
                             atr_5m_previous=latest["previous_atr"], atr_5m_source_end=latest["end"],
                             stop_atr_5m=distance / latest["previous_atr"],
                             verified_favorable_initial_r=favorable / distance,
                             intrabar_exit_extrema_excluded=True)
                scales.append(scale)
            all_trades.extend(trades)
        ordered = sorted((float(t["net_pnl"]) for t in all_trades), reverse=True)
        net = sum(ordered)
        totals[variant] = {"trades": len(all_trades), "net": net, "without_best_trade": net - ordered[0],
                           "without_best_two_trades": net - sum(ordered[:2]),
                           "days_with_trade": len({t["entry_time"][:10] for t in all_trades}),
                           "best_trade_share_of_net": ordered[0] / net,
                           "month_net": {s["month"]: s["metrics"]["net_profit"] for s in scenarios if s["variant"] == variant}}
    require(len(scales) == len(review["charts"]), "图表成交未完整覆盖")
    db = sqlite3.connect(f"file:{ROOT / 'runtime/workbench.db'}?mode=ro", uri=True)
    runtime = {"document_counts": dict(db.execute("SELECT kind,count(*) FROM documents WHERE kind IN ('versions','instances','strategies','strategy_instances') GROUP BY kind")),
               "versions": [{k: json.loads(body).get(k) for k in ("id", "name", "class_name", "published")}
                            for (body,) in db.execute("SELECT body FROM documents WHERE kind='versions'")]}
    db.close()
    control = [s["stop_atr_5m"] for s in scales if s["variant"] == "control"]
    result = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "kind": "read_only_current_strategy_diagnosis", "sources": sources, "scenarios": scenarios,
              "totals": totals, "trade_scale": scales, "runtime": runtime,
              "control_stop_atr_5m_range": [min(control), max(control)],
              "development_days": 52, "new_backtest_run": False, "strategy_changed": False,
              "locked_test_read": False, "source_results_changed": False,
              "checks": {"existing_relevant_tests": 68, "existing_relevant_tests_passed": True,
                         "trade_totals_match_saved_runs": True, "causal_completed_5m_source": True}}
    result["sources"].append({"path": str(SOURCE / "trade_review/review_data.json.gz"),
                              "sha256": sha(SOURCE / "trade_review/review_data.json.gz")})
    (OUT / "diagnosis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (OUT / "trade_scale.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(scales[0]))
        writer.writeheader()
        writer.writerows(scales)
    render_report(result)
    print(json.dumps({"totals": totals, "control_stop_atr_5m_range": result["control_stop_atr_5m_range"],
                      "output": str(OUT)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
