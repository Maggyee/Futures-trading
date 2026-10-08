"""Build a Chinese offline report from complete audited optimization runs."""

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import ResearchError
from .coverage_audit import rows
from .optimization_assessment import assess, promotion, read_scenario, totals
from .optimization_review import PLAN
from .reporting import write_json
from .storage import SpaceBudget, directory_bytes

LABELS = {"control": "K=2 原策略", "cost": "成本 / ATR ≤ 0.5", "breakeven": "1R 后保本保护",
          "pullback": "MA10 回踩确认", "afternoon": "午后 8 分钟重选", "combined": "成本过滤 + 保本保护"}


def pack(scenario):
    run = Path(scenario["directory"])
    slippage = json.loads((run/"config_snapshot.json").read_text())["strategy"]["slippage_ticks"]
    fields = ("id", "contract", "product", "group", "direction", "rank", "quantity", "entry_time", "exit_time",
              "entry_signal_time", "exit_reason", "entry_price", "exit_price", "net_pnl", "gross_pnl", "fees",
              "holding_minutes", "stop_price", "target_price", "slippage_cost_diagnostic")
    numeric = {"id", "rank", "quantity", "entry_price", "exit_price", "net_pnl", "gross_pnl", "fees",
               "holding_minutes", "stop_price", "target_price", "slippage_cost_diagnostic"}
    trades = []
    for original in scenario["trades"]:
        trade = {field: float(original[field]) if field in numeric else original[field] for field in fields}
        for name in ("entry_snapshot", "entry_protection", "trailing_exit", "pullback"):
            trade[name] = json.loads(original[name]) if original.get(name) else None
        tick = abs(trade["entry_price"]-trade["stop_price"]) / trade["entry_protection"]["stop_loss_ticks"]
        trade["cost_atr"] = trade["entry_protection"]["roundtrip_fee_and_slippage_ticks"] * tick / trade["entry_protection"]["signal_atr"]
        sign = 1 if trade["direction"] == "LONG" else -1
        trade["raw_exit_price"] = trade["exit_price"] + sign * slippage * tick
        trades.append(trade)
    eligible, filtered, potential_pullbacks, selected = 0, 0, 0, 0
    for row in rows(run/"signals.csv.gz"):
        filters = json.loads(row["filters"])
        if not filters["candidate"]:
            continue
        selected += 1
        intrinsic = all(value for name,value in filters.items() if name not in {"state", "cost"})
        if intrinsic:
            eligible += 1
            filtered += filters.get("cost") is False and row["execution_pass"] == "True"
            potential_pullbacks += bool(row.get("pullback"))
    cancellations = Counter(row.get("reason") for row in rows(run/"events.csv.gz") if row["action"] == "entry_cancelled")
    paths = {(p["contract"],p["entry_time"]): p for p in scenario["audit"]["paths"]}
    return {"month": scenario["month"], "variant": scenario["variant"], "directory": str(run),
            "window": scenario["window"], "metrics": scenario["metrics"], "trades": trades,
            "paths": [paths[(t["contract"],t["entry_time"])] for t in trades],
            "funnel": scenario["summary"]["signal_funnel"]["sequential"],
            "diagnostics": {"selected_observations": selected, "quality_eligible_observations": eligible,
                "cost_only_rejections": filtered, "eligible_pullback_observations": potential_pullbacks,
                "fill_cancellations": dict(cancellations),
                "independent_rejections": scenario["summary"]["signal_funnel"]["independent_rejections"],
                "breakeven_activated": sum(p["breakeven_activated"] for p in paths.values()),
                "trailing_activated": sum(p["activated"] for p in paths.values())},
            "audit_status": "passed", "optimization_checks": scenario["audit"]["optimization_checks"],
            "source_archive_audit": scenario["source_archive_audit"],
            "source_hashes": scenario["source_hashes"], "locked_test_read": False}


def changes(control, candidate):
    key = lambda t: (t["contract"],t["direction"],t["entry_signal_time"])
    left, right = ({key(t):t for t in scenario["trades"]} for scenario in (control,candidate))
    shared = left.keys() & right.keys()
    return {"month": candidate["month"], "variant": candidate["variant"],
            "added": [t for identity,t in right.items() if identity not in left],
            "removed": [t for identity,t in left.items() if identity not in right],
            "changed": [{"contract": right[k]["contract"], "direction": right[k]["direction"], "entry_signal_time": right[k]["entry_signal_time"],
                         "old_exit": left[k]["exit_time"], "new_exit": right[k]["exit_time"],
                         "old_net": float(left[k]["net_pnl"]), "new_net": float(right[k]["net_pnl"]),
                         "delta": float(right[k]["net_pnl"])-float(left[k]["net_pnl"])} for k in sorted(shared)
                         if left[k]["exit_time"] != right[k]["exit_time"] or abs(float(left[k]["net_pnl"])-float(right[k]["net_pnl"])) > 1e-6],
            "shared_count": len(shared), "shared_net_delta": sum(float(right[k]["net_pnl"])-float(left[k]["net_pnl"]) for k in shared)}


def build(plan_path=PLAN):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    assessment, scenarios = assess(plan_path)
    if assessment["combined_eligible"]:
        combined = [read_scenario(plan_path,plan,month,"combined") for month in plan["baselines"]]
        control = [s for s in scenarios if s["variant"] == "control"]
        assessment["totals"]["combined"] = totals(combined)
        assessment["promotion"]["combined"] = promotion(control,combined,plan["promotion"]["minimum_trade_count"])
        assessment["inputs"] += [{"month": s["month"], "variant": s["variant"], "directory": s["directory"],
                                  "hashes": s["source_hashes"]} for s in combined]
        scenarios += combined
        assessment["combined_status"] = "completed_and_audited"
    else:
        assessment["combined_status"] = "not_run_preregistered_gate_failed"
    roots = Path(plan["output"]).parent
    coverage = json.loads((roots/"report_data.json").read_text())
    controls = {s["month"]:s for s in scenarios if s["variant"] == "control"}
    modified = [changes(controls[s["month"]],s) for s in scenarios if s["variant"] != "control"]
    for change in modified:
        for name in ("added", "removed"):
            change[name] = [{field: row[field] for field in ("contract", "direction", "entry_time", "entry_signal_time", "exit_time", "exit_reason", "net_pnl")} for row in change[name]]
    cfg = json.loads((Path(scenarios[0]["directory"])/"config_snapshot.json").read_text())
    report = {"created": datetime.now().astimezone().isoformat(), "labels": LABELS, "assessment": assessment,
              "scenarios": [pack(s) for s in scenarios], "changes": modified, "coverage": coverage["coverage"],
              "strategy": cfg["strategy"], "risk": cfg["risk"], "research_days": sum(s["metrics"]["daily_count"] for s in controls.values()),
              "sample_note": coverage["sample_note"], "local_bytes": directory_bytes(plan["budget"]["roots"]),
              "policy": json.loads((plan_path.parent/"cloud_policy.json").read_text()),
              "locked_test_read": False, "live_trading_changed": False}
    passed = [LABELS[v] for v,d in assessment["promotion"].items() if d["passed"]]
    headline = "、".join(passed) + "通过本轮预定诊断标准" if passed else "四项改动均未通过全部预定诊断标准"
    payload = json.dumps(report,ensure_ascii=False,allow_nan=False).replace("<", "\\u003c")
    document = TEMPLATE.replace("__DATA__",payload).replace("__HEADLINE__",html.escape(headline)).replace("__BUILT__",html.escape(report["created"][:16].replace("T"," ")))
    budget = SpaceBudget(plan["budget"])
    target = roots/"optimization_review.html"
    budget.check(target,reserve=len(document.encode()))
    temporary = target.with_suffix(".html.partial")
    temporary.write_text(document,encoding="utf-8")
    temporary.replace(target)
    write_json(roots/"optimization_report_data.json",report,budget)
    write_json(Path(plan["output"])/"final_assessment.json",assessment,budget)
    print(json.dumps({"report":str(target),"promotion":{v:d["passed"] for v,d in assessment["promotion"].items()},"combined_status":assessment["combined_status"]},ensure_ascii=False))
    return target


TEMPLATE = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>策略优化验证 · 按顺序推进</title>
<style>
:root{--ink:#18343a;--muted:#5a7175;--accent:#147566;--bad:#ad4233;--line:#d6e3dc;--paper:#f3f6f1}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--paper);font:16px/1.7 "Noto Sans CJK SC","Microsoft YaHei",sans-serif}main{max-width:1200px;margin:auto;padding:38px 24px 70px}h1{font-size:clamp(25px,4vw,40px);line-height:1.3;margin:14px 0}h2{font-size:23px;margin:0 0 16px}h3{font-size:17px;margin:18px 0 8px}p{margin:10px 0}section{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px;margin:20px 0;min-width:0}.eyebrow{color:var(--accent);letter-spacing:.1em;font-size:13px}.muted{font-size:14px;color:var(--muted)}.notice{padding:16px 20px;border-left:4px solid var(--accent);background:#e6efe6;border-radius:5px;margin:22px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{padding:20px;background:#fff;border:1px solid var(--line);border-radius:12px;min-width:0}.value{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}.label{font-size:13px;color:var(--muted)}.table-wrap{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}th,td{white-space:nowrap;padding:11px 10px;border-bottom:1px solid var(--line);text-align:right}th{color:var(--muted);font-weight:500}th:first-child,td:first-child{text-align:left}td.wrap{white-space:normal;min-width:200px}.pos{color:var(--accent)}.neg{color:var(--bad)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.grid>div{min-width:0}.rule{background:#f6f8f4;border-radius:10px;padding:16px}.rule h3{margin-top:0}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}.controls select{min-width:0;max-width:100%}select,button{font:inherit;padding:8px 12px;border:1px solid #9db7ac;color:var(--ink);background:#fff;border-radius:8px;cursor:pointer}button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #dfac61;outline-offset:3px}.stats{display:flex;gap:22px;flex-wrap:wrap;margin:16px 0}.stats strong{display:block;font-variant-numeric:tabular-nums}.pill{display:inline-block;border-radius:20px;padding:2px 9px;font-size:12px;background:#e7efe5}.chart{width:100%;height:auto;display:block}.plot{min-width:670px}.path-plot{min-width:700px}ul,ol{padding-left:23px}li{margin:7px 0}details{margin:16px 0}summary{cursor:pointer}a{color:var(--accent)}.footer{font-size:12px;overflow-wrap:anywhere;color:var(--muted);margin-top:26px}.empty{padding:20px;color:var(--muted)}@media(max-width:700px){main{padding:24px 12px 45px}.cards{grid-template-columns:1fr 1fr}.card,section{padding:16px}.grid{grid-template-columns:1fr}.value{font-size:clamp(15px,4.8vw,23px)}table{font-size:12px}.controls select,.controls button{font-size:14px;padding:8px}}
</style></head><body><main>
<div class="eyebrow">K=2 · 单项对照 · 完整组合回放 · 预定标准</div><h1>按顺序验证：收益、回撤与入场数量</h1><p class="muted">7 月、8 月完整窗口与 9 月14—23 日 · 生成于 __BUILT__</p>
<div class="notice"><strong>__HEADLINE__</strong><p id="headline-detail"></p><p class="muted">各窗口独立以100万元起步，跨窗口金额为各次回放净收益之和。本轮使用已查看的开发样本，属于问题诊断；9 月24—30 日继续锁定。通过这里的标准，只获得下一轮验证资格。</p></div>
<div class="cards" id="overview"></div>
<section><h2 id="results-title">完整结果</h2><p>原策略作为统一对照，依次只增加成本过滤、1R 保本保护、MA10 回踩确认或午后候选刷新。每组均完整重算入场、退出、资金占用和再入场。</p><div class="table-wrap"><table><thead><tr><th>方案</th><th>成交 / 笔</th><th>每交易日 / 笔</th><th>净收益 / 元</th><th>相对原策略 / 元</th><th>去掉最好单 / 元</th><th>最大窗口回撤 / 元</th><th>本轮晋级</th></tr></thead><tbody id="totals"></tbody></table></div><p class="muted">“最大窗口回撤”是三个独立回放中最大的盘中回撤；跨窗口没有拼接账户净值。去掉最好单仅剔除该笔结果，是收益集中度诊断，未重算后续资金状态。</p><div class="table-wrap"><div id="total-chart" class="plot"></div></div></section>
<section><h2>从结果看，问题在哪</h2><div class="grid" id="diagnosis"></div><p class="muted">这些是本轮已有窗口的诊断。下一项验证优先尝试排名前的执行资格与最小手数检查，并把保本单项保留作成交数量对照。下面的改进顺序区分已测试规则和待验证假设。</p></section>
<section><h2>按原定顺序判断是否晋级</h2><p>实验开始前固定了五项条件：总净收益提高；每个窗口净收益均不降低；每个窗口回撤均不扩大；剔除最大盈利单后的净收益不变差；总成交不少于8笔。阈值和斜率区间保持冻结。</p><div class="table-wrap"><table><thead><tr><th>验证项</th><th>总净收益提高</th><th>各窗口收益</th><th>各窗口回撤</th><th>去掉最好单</th><th>不少于8笔</th><th>结论</th></tr></thead><tbody id="promotion"></tbody></table></div><p id="combined-note"></p><ol id="next-steps"></ol></section>
<section><h2>三个窗口分别发生了什么</h2><div class="table-wrap"><table><thead><tr><th>窗口 / 方案</th><th>成交 / 笔</th><th>净收益 / 元</th><th>相对原策略 / 元</th><th>最大回撤 / 元</th><th>回撤变化 / 元</th><th>手续费 / 元</th><th>胜率</th></tr></thead><tbody id="windows"></tbody></table></div></section>
<section><h2>逐笔检查与入场数量</h2><div class="controls"><label for="month">窗口</label><select id="month"><option value="2026-07">7 月 · 23 个交易日</option><option value="2026-08">8 月 · 21 个交易日</option><option value="2026-09">9 月14—23 日 · 8 个交易日</option></select><label for="variant">方案</label><select id="variant"></select><button id="export" type="button">导出本组交易表</button></div><div class="stats" id="detail-stats"></div><div class="table-wrap"><div id="equity-chart" class="plot"></div></div><p class="muted">每日收盘累计净收益：绿色为当前方案，灰色为原策略。表中的回撤按盘中权益计算。</p><h3>入场筛选和实际成交</h3><div class="table-wrap"><table><thead><tr><th>选中候选分钟观察</th><th>原质量条件通过</th><th>质量达标后成本拒绝</th><th>通过质量且有回踩</th><th>完整触发</th><th>执行资料通过</th><th>风险通过</th><th>实际成交</th></tr></thead><tbody id="funnel"></tbody></table></div><p class="muted">分钟观察会重复，不能当作独立机会或交易。成本拒绝数限于质量达标且有执行资料的观察，未计持仓状态和风险限制；质量条件通过数排除持仓状态和新增成本条件；回踩方案的质量通过还要等待确认。所有比较在此统一为选中候选的观察范围。</p><p id="entry-note"></p><h3>相对原策略的成交变化</h3><div id="changes"></div><h3>逐笔交易</h3><div class="table-wrap"><table><thead><tr><th>合约 / 方向</th><th>入场 → 出场</th><th>排名 / 手数</th><th>1分 / 5分斜率</th><th>成本 / ATR</th><th>初始止损 / 跳</th><th>持仓 / 分钟</th><th>退出</th><th>净收益 / 元</th></tr></thead><tbody id="trades"></tbody></table></div><div class="controls"><label for="trade-select">保护路径</label><select id="trade-select"></select></div><div class="table-wrap"><div id="trade-chart" class="path-plot"></div></div><p class="muted" id="trade-note"></p><details><summary>回踩时间与午后排名核验</summary><div id="entry-audit"></div></details></section>
<section><h2>目前策略与这轮改动的具体含义</h2><div class="grid"><div class="rule"><h3>候选与入场斜率</h3><p>前一交易日持仓量选出实际代表合约，日盘开盘前8个完成分钟按涨跌幅排名。每个资金组、每个方向取前2名。候选再检查执行资格，缺资料时不补位。</p><p>1分钟MA20回看5根：方向斜率 0.035685—0.180626；5分钟MA20回看3根：0.039664—0.209563。两周期均线位移至少1跳。斜率是MA20变化 / 根数 / 上一根ATR，衡量均线相对波动的变化。</p><p>同时要求1、5、15分钟趋势，当前交易日15分钟确认，均价、持仓量、效率≥0.45，至少3次价格变化 / 10分钟，方向位移≥1ATR且≥2跳；过滤过强冲击和乖离。下一可交易分钟开盘计1跳不利滑点，再检查价格边界。</p></div><div class="rule"><h3>一跳、固定止损与追踪</h3><p>一跳是最小报价变动，金额还要乘合约价值与手数。例如LC一跳20元/吨、1吨/手，即单手20元；螺纹一跳1元/吨、10吨/手，即单手10元。</p><p>当前初始止损取训练跳数、1倍信号ATR、2倍往返费用和滑点的最大值，并按风险预算缩减手数。逐笔止损已放大，不能只看训练表的“一跳两跳”。</p><p>初始目标保持原比例，用于启动追踪。有成交量的完成分钟到达目标后，按最有利价格回撤2倍上一根ATR收紧保护线，从下一分钟生效。放量≥2.5倍且价格走弱、MA40穿越与时间退出规则继续执行。</p></div><div class="rule"><h3>成本过滤与1R保本</h3><p>成本过滤：开仓时已知的往返手续费折成价格距离，加上双边滑点，再除以信号1分钟ATR；只接受≤0.5。实际开盘报价仍须复查。它可能排除成本较高的单子，也会改变后续触发时机。</p><p>1R为成交价到初始止损的价格距离。完成K线的有利极值到达1R后，把保护线收紧到能覆盖已付开仓费、预计平今费和退出滑点的报价，再按最小跳数取整。原目标启动的2ATR追踪继续运行。跳空成交仍可能亏损，保本线不是保证。</p></div><div class="rule"><h3>回踩与午后刷新</h3><p>MA10回踩：最近3根已完成K线中，价格靠近MA10，容差取1跳与0.2ATR的较大值；回踩前收盘须在MA10和MA20趋势侧。当前收盘突破前根高点或低点后确认；同一回踩事件每天仅使用一次。</p><p>午后刷新：各市场午后第一段开盘后等待8个完整分钟，只用这一段涨跌幅重排，K仍为2，并替换该时段入场候选。原持仓继续按原规则管理，风险预算相同。缺分钟或缺持仓量等数据的候选拒绝参与。</p></div></div><p class="muted">每个窗口资金100万元；单笔计划风险0.2%，组合1%，保证金30%，商品与金融各50%，最多5个持仓。各单项实验相互独立，未把后一个改动自动叠在前一个上。</p></section>
<section><h2>数据、审计与云盘</h2><div class="grid"><div><h3>复核范围</h3><p id="audit-summary"></p><p>7、8月各覆盖83个有行情品种，执行资料支持55个品种；其余候选按原规则拒绝。9月沿用原数据与执行资料。月份之间可执行范围不同；方案之间使用同月同一行情与规则。</p><p>独立核对费用、入场均线和斜率、风险预算、完整保护路径、成交及盘中回撤；回踩重算历史收盘均线，午后排名从原始8个分钟重建，成本与保本价格另行计算。原策略的控制回放精确复现成交、权益和候选排名。</p></div><div><h3>自动同步与限额</h3><p id="storage-note"></p><p>每15分钟自动同步至 Google Drive：quant_backup / vnpy-ctp / research-v1。新报告副本为 reports / 2026-10-05 / optimization_review.html。云端内容先校验再发布；读取校验后进入受限缓存，凭据不进入备份。</p><p>自动同步有每日上传和读取合计限额2GiB、云端50GiB、读取缓存1GiB。达到额度后保留进度，等待下一天继续。</p></div></div><details><summary>结果的适用范围与复现记录</summary><p>7、8月参数是在9月确定后回看使用；9月14—23日也已多次查看。本轮不能证明独立样本外有效。未读取9月24—30日的锁定测试，也未修改实盘配置。</p><p>费用使用交易所参考值，账户加收未知；均价在缺成交额时用典型价近似。分钟OHLC不能还原盘口、排队或所有分钟内先后。执行资料仍包含注明的历史连续性假设。</p><p>方案使用重叠行情和交易，各组不能累加为独立样本。回踩为零成交时，只说明它在这些条件下未产生入场，不能判断其盈利能力。</p><div id="fingerprints" class="footer"></div></details></section>
<div class="footer">本页可离线打开、打印和导出实际交易表。<a href="strategy_coverage_review.html">查看此前的覆盖扩展与K=2/K=5报告</a>。冻结实验声明继续保留。</div>
</main><script id="report-data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('report-data').textContent),A=D.assessment,V=Object.keys(A.totals),base=A.totals.control;
const n=(v,d=2)=>v==null?'—':(Number(v)===0?0:Number(v)).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}),money=v=>`<span class="${v<0?'neg':'pos'}">${n(v)}</span>`,pct=v=>v==null?'—':n(v*100,1)+'%',yes=v=>v?'通过':'未通过';
const el=id=>document.getElementById(id),reason={fixed_stop:'初始止损',breakeven_stop:'保本保护',trailing_stop:'追踪保护',volume:'放量走弱',ma40_cross:'均线穿越',ma40_approach:'接近均线',time_force:'时间平仓',session_close:'收盘平仓',break_close:'休市平仓'},shortTime=t=>t.slice(5,16).replace('T',' ');
const promoted=V.filter(v=>v!=='control'&&A.promotion[v].passed),best=[...promoted].sort((a,b)=>A.totals[b].net-A.totals[a].net)[0];
el('results-title').textContent='先看 '+V.length+' 组完整结果';
el('headline-detail').textContent=`原策略 ${base.trade_count} 笔、净收益 ${n(base.net)} 元。${best?`通过标准中净收益最高的是${D.labels[best]}，${A.totals[best].trade_count} 笔、${n(A.totals[best].net)} 元；去掉最好单后 ${n(A.totals[best].without_best_trade)} 元。`:''}${V.every(v=>A.totals[v].trade_count<=base.trade_count)?'本轮没有方案增加成交，入场偏少仍待解决。':''}${promoted.length?'通过诊断标准的方案仍需冻结后再验证新时段。':'继续保留原策略作为对照，按下表定位失败原因。'}`;
el('overview').innerHTML=[[n(base.net),'原策略净收益 / 元'],[n(base.without_best_trade),'原策略去掉最好单 / 元'],[n(D.research_days,0),'实际研究交易日'],[n(promoted.length,0)+' / '+n(V.length-1,0),'通过全部诊断条件的方案']].map(([v,l])=>`<div class="card"><div class="value">${v}</div><div class="label">${l}</div></div>`).join('');
el('totals').innerHTML=V.map(v=>{let t=A.totals[v];return `<tr><td>${D.labels[v]}</td><td>${t.trade_count}</td><td>${n(t.trade_count/D.research_days,3)}</td><td>${money(t.net)}</td><td>${v==='control'?'—':money(t.net-base.net)}</td><td>${money(t.without_best_trade)}</td><td>${n(t.max_window_drawdown)}</td><td>${v==='control'?'统一对照':yes(A.promotion[v].passed)}</td></tr>`}).join('');
el('promotion').innerHTML=V.filter(v=>v!=='control').map(v=>{let d=A.promotion[v];return `<tr><td>${D.labels[v]}</td>${Object.values(d.checks).map(x=>`<td>${yes(x)}</td>`).join('')}<td>${d.passed?'进入下一轮验证':'保留为诊断结果'}</td></tr>`}).join('');
el('combined-note').textContent=A.combined_eligible?'成本过滤与保本保护各自通过门槛，已按预定条件补充组合回放。':'按预定条件，只有成本过滤和保本保护各自通过全部标准才组合。本轮门槛未满足，组合实验未启动。';
const steps=V.filter(v=>v!=='control').map(v=>{let t=A.totals[v],d=A.promotion[v],fail=Object.entries(d.checks).filter(([,x])=>!x).map(([k])=>({total_net_improves:'总净收益未提高',each_window_net_not_worse:'部分窗口收益变差',each_window_drawdown_not_worse:'部分窗口回撤扩大',without_best_trade_net_not_worse:'去掉最好单后变差',minimum_trade_count:'成交不足8笔'}[k]));return `<li><strong>${D.labels[v]}</strong>：净收益变化 ${n(d.total_net_delta)} 元，成交变化 ${d.total_trade_delta>=0?'+':''}${d.total_trade_delta} 笔。${d.passed?'保留固定规则，优先验证新的完整时段与账户实际成本。':'未通过：'+fail.join('、')+'。'+(v==='pullback'&&t.trade_count<8?'应先排查回踩触达与收盘突破是否过稀，再另行声明新假设。':v==='afternoon'?'检查新增与被替换成交的净收益及午后执行覆盖。':v==='cost'?'检查成本门槛是否只减少成交，以及拒绝后的新触发时机。':'检查保本是否过早打断后续趋势，以及跳空后的实际净收益。')}</li>`});
el('next-steps').innerHTML=steps.join('')+'<li><strong>先解决候选不可执行的问题</strong>：下一项另立声明，尝试把已知执行资格、最小开仓手数的计划风险和保证金检查放在K=2排名之前，避免无法成交的候选占名额。只使用当时已知资料，成交时仍复查；保留斜率上下限和原风险预算。</li><li><strong>再验证止损后的同向冷却</strong>：7月27日两笔LC原策略亏损合计3443.06元，均未到1R，保本保护没有覆盖到。可另立单项假设延长同向冷却，检查重复损失减少的同时是否错过后续盈利。本轮未测试这条新规则。</li><li><strong>最后验证新的完整时段</strong>：冻结候选规则后扩大未查看过的时间段，并加入实际账户手续费。仅有一笔盈利支撑时继续观察；9月24—30日按既定流程保留锁定。</li>';
el('windows').innerHTML=D.scenarios.map(s=>{let c=D.scenarios.find(x=>x.month===s.month&&x.variant==='control');return `<tr><td>${s.month.slice(5)}月 / ${D.labels[s.variant]}</td><td>${s.metrics.trade_count}</td><td>${money(s.metrics.net_profit)}</td><td>${s.variant==='control'?'—':money(s.metrics.net_profit-c.metrics.net_profit)}</td><td>${n(s.metrics.max_drawdown)}</td><td>${s.variant==='control'?'—':n(s.metrics.max_drawdown-c.metrics.max_drawdown)}</td><td>${n(s.metrics.fees)}</td><td>${pct(s.metrics.win_rate)}</td></tr>`}).join('');
function totalChart(){let vals=V.map(v=>A.totals[v].net),lo=Math.min(0,...vals),hi=Math.max(0,...vals),pad=Math.max(1,(hi-lo)*.12);lo-=pad;hi+=pad;let y=v=>230-(v-lo)/(hi-lo)*170,w=850/V.length;el('total-chart').innerHTML=`<svg class="chart" role="img" aria-label="各方案跨窗口净收益，单位元" viewBox="0 0 1000 320"><text x="76" y="22" font-size="15">跨窗口净收益 / 元</text><line x1="76" x2="950" y1="${y(0)}" y2="${y(0)}" stroke="#c8d4ce"/><text x="68" y="62" text-anchor="end" font-size="12">${n(hi,0)}</text><text x="68" y="232" text-anchor="end" font-size="12">${n(lo,0)}</text>${V.map((v,i)=>{let value=A.totals[v].net,x=90+i*w;return `<g><rect x="${x}" y="${Math.min(y(value),y(0))}" width="${w*.6}" height="${Math.max(1,Math.abs(y(value)-y(0)))}" fill="${v==='control'?'#a9bab2':'#147566'}"><title>${D.labels[v]} ${n(value)}元</title></rect><text x="${x+w*.3}" y="${value>=0?y(value)-8:y(value)+18}" text-anchor="middle" font-size="12">${n(value,0)}</text><text x="${x+w*.3}" y="277" text-anchor="middle" font-size="12">${D.labels[v]}</text></g>`}).join('')}<text x="950" y="310" text-anchor="end" font-size="12">独立窗口之和；非连续账户收益</text></svg>`}totalChart();
const controlTrades=D.scenarios.filter(s=>s.variant==='control').flatMap(s=>s.trades),bands=D.strategy.slope_band.timeframes;
const slopeOK=controlTrades.every(t=>Object.entries(bands).every(([tf,b])=>{const x=t.entry_snapshot.slope_band[tf].signed_atr_per_bar;return x>=b.min_atr_per_bar-1e-12&&x<=b.max_atr_per_bar+1e-12}));
const stops=controlTrades.map(t=>t.entry_protection.stop_loss_ticks),july=D.scenarios.find(s=>s.variant==='control'&&s.month==='2026-07'),augustPullback=D.scenarios.find(s=>s.variant==='pullback'&&s.month==='2026-08'),r=augustPullback.diagnostics.independent_rejections;
const quickReasons={};controlTrades.filter(t=>t.holding_minutes<=5).forEach(t=>quickReasons[t.exit_reason]=(quickReasons[t.exit_reason]||0)+1);
el('diagnosis').innerHTML=`<div class="rule"><h3>斜率与实际保护距离</h3><p>对照组${controlTrades.length}笔成交${slopeOK?'均守住现有1分钟、5分钟斜率上下限':'存在斜率核验异常，应先核查'}。实际初始止损${Math.min(...stops)}—${Math.max(...stops)}跳；仍有${base.quick_trades}笔在5分钟内退出：${Object.entries(quickReasons).map(([k,v])=>(reason[k]||k)+' '+v+'笔').join('、')}。逐笔表可检查入场时数值；肉眼看到的单根K线形态与MA20归一化斜率需要分别判断。</p></div><div class="rule"><h3>费用与保护各解决什么</h3><p>原策略毛收益${n(base.gross)}元，已包含双边滑点；手续费${n(base.fees)}元，最终净收益${n(base.net)}元。成本过滤减少高成本入场；1R保本改善部分已出现浮盈的亏损，也可能提前结束后续盈利。保本保护需要先达到1R。本轮7月两笔LC亏损未到1R，且未被成本过滤排除，仍然保留。</p></div><div class="rule"><h3>入场少有两道瓶颈</h3><p>7月${n(july.diagnostics.selected_observations,0)}个候选分钟观察中，只有${july.diagnostics.quality_eligible_observations}个通过原质量与入场时点条件，最后成交${july.trades.length}笔。8月回踩完整触发${augustPullback.funnel.entry_trigger}次，单笔风险与分组保证金各拒绝${r.single_trade_risk||0}次，实际成交0笔。同一机会可触发多个拒绝条件。分钟观察存在重复，不代表独立机会数量。</p></div><div class="rule"><h3>盈利仍集中在最好单</h3><p>${best?`净收益最高的通过方案为${D.labels[best]}，${A.totals[best].trade_count}笔、${n(A.totals[best].net)}元；去掉最大盈利${n(A.totals[best].best_trade)}元后，剩余${n(A.totals[best].without_best_trade)}元。`:'本轮没有方案通过全部预定标准。'}${V.every(v=>A.totals[v].trade_count<=base.trade_count)?'所有改动的成交数量都未超过原策略。':''}继续验证需要更多完整时段，并同时检查成交数量与盈利集中度。</p></div>`;
el('variant').innerHTML=V.map(v=>`<option value="${v}">${D.labels[v]}</option>`).join('');el('variant').value=best||'control';
const current=()=>D.scenarios.find(s=>s.month===el('month').value&&s.variant===el('variant').value);
function equity(s,c){let vals=[0,...s.metrics.daily.map(d=>d.equity-1000000),...c.metrics.daily.map(d=>d.equity-1000000)],lo=Math.min(...vals),hi=Math.max(...vals),pad=Math.max(1,(hi-lo)*.1);lo-=pad;hi+=pad;let x=i=>78+i/s.metrics.daily_count*870,y=v=>225-(v-lo)/(hi-lo)*170;let draw=(data,color)=>`<path d="${[0,...data.map(d=>d.equity-1000000)].map((v,i)=>(i?'L':'M')+x(i)+','+y(v)).join(' ')}" fill="none" stroke="${color}" stroke-width="2"/>`;
el('equity-chart').innerHTML=`<svg class="chart" role="img" aria-label="${s.month}每日收盘累计净收益对比" viewBox="0 0 1000 295"><text x="78" y="24" font-size="15">每日收盘累计净收益 / 元</text><text x="68" y="58" text-anchor="end" font-size="12">${n(hi,0)}</text><text x="68" y="228" text-anchor="end" font-size="12">${n(lo,0)}</text><line x1="78" x2="948" y1="${y(0)}" y2="${y(0)}" stroke="#d1dcd6"/>${draw(c.metrics.daily,'#b2bdb7')}${draw(s.metrics.daily,'#147566')}${s.metrics.daily.map((d,i)=>`<circle cx="${x(i+1)}" cy="${y(d.equity-1000000)}" r="4" fill="#147566"><title>${d.date} ${n(d.equity-1000000)}元</title></circle>`).join('')}<text x="78" y="270" font-size="12">${s.window.start}</text><text x="948" y="270" text-anchor="end" font-size="12">${s.window.end}</text></svg>`}
function drawTrade(){const s=current(),i=Number(el('trade-select').value),t=s.trades[i],p=s.paths[i];if(!t||!p){el('trade-chart').innerHTML='<p class="empty">本组没有成交保护路径。</p>';el('trade-note').textContent='';return}let points=p.path,be=t.trailing_exit.breakeven_price,vals=[t.entry_price,t.stop_price,t.target_price,t.exit_price,t.raw_exit_price,...(be==null?[]:[be]),...points.flatMap(r=>r.exit?[r.open,r.stop_at_open]:[r.high,r.low,r.stop_at_open])],lo=Math.min(...vals),hi=Math.max(...vals),pad=Math.max(1,(hi-lo)*.1);lo-=pad;hi+=pad;let x=j=>90+j/Math.max(1,points.length-1)*850,y=v=>260-(v-lo)/(hi-lo)*200,w=Math.min(14,650/points.length);let candles=points.map((r,j)=>{let cx=x(j);return r.exit?`<g><circle cx="${cx}" cy="${y(r.open)}" r="3" fill="#aebfc7"><title>${r.start} 退出分钟开盘 ${n(r.open)}；排除本分钟后续极值</title></circle><circle cx="${cx}" cy="${y(t.raw_exit_price)}" r="5" fill="none" stroke="#b3915f" stroke-width="2"><title>${t.exit_time} 滑点前退出价 ${n(t.raw_exit_price)}</title></circle><circle class="exit-fill" cx="${cx}" cy="${y(t.exit_price)}" r="4" fill="#147566"><title>${t.exit_time} 回测成交价 ${n(t.exit_price)}，已含退出滑点</title></circle></g>`:`<g><title>${r.start} 开${n(r.open)} 高${n(r.high)} 低${n(r.low)} 收${n(r.close)}；已知保护 ${n(r.stop_at_open)}</title><line x1="${cx}" x2="${cx}" y1="${y(r.high)}" y2="${y(r.low)}" stroke="#477182"/><rect x="${cx-w/2}" y="${Math.min(y(r.open),y(r.close))}" width="${w}" height="${Math.max(1,Math.abs(y(r.close)-y(r.open)))}" fill="${r.close>=r.open?'#477182':'#aebfc7'}"/></g>`}).join('');
el('trade-chart').innerHTML=`<svg class="chart" role="img" aria-label="${t.contract}成交与当时已知保护路径" viewBox="0 0 1000 330"><text x="90" y="22" font-size="15">${t.contract} · K线 / 红色已知保护 / 灰色追踪启动价</text><text x="80" y="63" text-anchor="end" font-size="12">${n(hi,1)}</text><text x="80" y="263" text-anchor="end" font-size="12">${n(lo,1)}</text><line x1="90" x2="940" y1="${y(t.entry_price)}" y2="${y(t.entry_price)}" stroke="#147566" stroke-dasharray="4 4"/><line x1="90" x2="940" y1="${y(t.target_price)}" y2="${y(t.target_price)}" stroke="#9aaba2" stroke-dasharray="5 4"/>${be==null?'':`<line x1="90" x2="940" y1="${y(be)}" y2="${y(be)}" stroke="#b3915f" stroke-dasharray="2 4"><title>计划保本线 ${n(be)}，须在1R完成确认后才生效</title></line>`}${candles}<path d="${points.map((r,j)=>(j?'L':'M')+x(j)+','+y(r.stop_at_open)).join(' ')}" fill="none" stroke="#ad4233" stroke-width="2"/><text x="90" y="301" font-size="12">${points[0].start.slice(11,16)}</text><text x="940" y="301" text-anchor="end" font-size="12">${t.exit_time.slice(11,16)}</text></svg>`;
el('trade-note').textContent=`入场 ${n(t.entry_price)}，退出 ${n(t.exit_price)}，净收益 ${n(t.net_pnl)} 元。初始止损 ${t.entry_protection.stop_loss_ticks} 跳；持仓 ${t.holding_minutes} 个交易分钟；1R保本${p.breakeven_activated?'已启动':'未启动'}，目标追踪${p.activated?'已启动':'未启动'}。红线是本分钟开盘时已知保护，更新从下一分钟生效。完成持仓K线的有利毛浮盈约 ${n(p.favorable_gross_cny)} 元，回吐至未加滑点的退出价约 ${n(p.giveback_to_raw_exit_cny)} 元；该极值不是可兑现净收益。横轴跳过非交易时段；退出分钟灰点为开盘，金色空心点为滑点前退出价，绿点为含滑点的回测成交价，排除退出后的高低点。${t.pullback?' 回踩事件 '+shortTime(t.pullback.event)+'，确认时间 '+shortTime(t.entry_signal_time)+'。':''}`;}
function render(){const s=current(),c=D.scenarios.find(x=>x.month===s.month&&x.variant==='control'),m=s.metrics,d=s.diagnostics;
el('detail-stats').innerHTML=`<div><strong>${money(m.net_profit)} 元</strong>净收益</div><div><strong>${m.trade_count} 笔</strong>成交</div><div><strong>${n(m.max_drawdown)} 元</strong>盘中回撤</div><div><strong>${n(m.fees)} 元</strong>手续费</div>`;equity(s,c);
const f=s.funnel;el('funnel').innerHTML='<tr>'+[d.selected_observations,d.quality_eligible_observations,d.cost_only_rejections,d.eligible_pullback_observations,f.entry_trigger,f.execution_pass,f.risk_pass,f.actual_fill].map(v=>`<td>${n(v||0,0)}</td>`).join('')+'</tr>';
el('entry-note').textContent=`本组保本启动 ${d.breakeven_activated} 笔、目标追踪启动 ${d.trailing_activated} 笔。开盘尝试取消 ${Object.values(d.fill_cancellations).reduce((a,b)=>a+b,0)} 次，其中成本复查拒绝 ${d.fill_cancellations.fill_cost_recheck||0} 次。独立风险拒绝记录：单笔风险 ${d.independent_rejections.single_trade_risk||0} 次、分组保证金 ${d.independent_rejections.group_margin||0} 次；同一次机会可能同时触发，次数不能相加为交易数。`;
const change=D.changes.find(x=>x.month===s.month&&x.variant===s.variant);el('changes').innerHTML=change?`<p>新增 ${change.added.length} 笔（净收益 ${money(change.added.reduce((a,t)=>a+Number(t.net_pnl),0))} 元）；原策略独有 ${change.removed.length} 笔（${money(change.removed.reduce((a,t)=>a+Number(t.net_pnl),0))} 元）；相同入场 ${change.shared_count} 笔，净收益变化 ${money(change.shared_net_delta)} 元。</p>`+(change.changed.length?'<div class="table-wrap"><table><thead><tr><th>相同入场</th><th>原退出</th><th>本组退出</th><th>原净收益 / 元</th><th>本组净收益 / 元</th><th>变化 / 元</th></tr></thead><tbody>'+change.changed.map(t=>`<tr><td>${t.contract} ${shortTime(t.entry_signal_time)}</td><td>${shortTime(t.old_exit)}</td><td>${shortTime(t.new_exit)}</td><td>${money(t.old_net)}</td><td>${money(t.new_net)}</td><td>${money(t.delta)}</td></tr>`).join('')+'</tbody></table></div>':''):'<p class="muted">原策略作为统一对照。</p>';
el('trades').innerHTML=s.trades.length?s.trades.map(t=>`<tr><td>${t.contract} ${t.direction==='LONG'?'多':'空'}</td><td>${shortTime(t.entry_time)} → ${t.exit_time.slice(11,16)}</td><td>${t.rank} / ${t.quantity}</td><td>${n(t.entry_snapshot.slope_band['1m'].signed_atr_per_bar,4)} / ${n(t.entry_snapshot.slope_band['5m'].signed_atr_per_bar,4)}</td><td>${n(t.cost_atr,3)}</td><td>${t.entry_protection.stop_loss_ticks}</td><td>${t.holding_minutes}</td><td>${reason[t.exit_reason]||t.exit_reason}</td><td>${money(t.net_pnl)}</td></tr>`).join(''):'<tr><td colspan="9">本组无成交。</td></tr>';
el('trade-select').innerHTML=s.trades.map((t,i)=>`<option value="${i}">${t.contract} ${shortTime(t.entry_time)} · ${reason[t.exit_reason]||t.exit_reason}</option>`).join('');drawTrade();
const checks=s.optimization_checks;el('entry-audit').innerHTML=(checks.pullback?`<p>独立核验 ${checks.pullback.entries_checked} 笔回踩成交，重算MA10与MA20，检查已完成事件、前根高低点突破与单次消费。</p>`:'')+(checks.afternoon?`<p>独立重建 ${checks.afternoon.cohorts_checked} 个午后时段、${checks.afternoon.candidates_checked} 个候选排名；核验 ${checks.afternoon.afternoon_entries_checked} 笔午后成交。缺数据的候选 ${checks.afternoon.unavailable.length} 个，涨跌幅为零 ${checks.afternoon.flat.length} 个；只使用首8个分钟。</p>`:'')+(!checks.pullback&&!checks.afternoon?`<p>核对原固有快照 ${checks.journal.intrinsic_observations_checked} 行；组合状态重新计算。成本核算 ${checks.journal.cost_observations_checked} 行。</p>`:'');}
el('month').addEventListener('change',render);el('variant').addEventListener('change',render);el('trade-select').addEventListener('change',drawTrade);
el('export').addEventListener('click',()=>{const s=current(),fields=['contract','direction','entry_time','exit_time','rank','quantity','entry_price','exit_price','exit_reason','holding_minutes','fees','net_pnl','cost_atr'];let body='\\ufeff'+fields.join(',')+'\\r\\n'+s.trades.map(t=>fields.map(k=>t[k]).join(',')).join('\\r\\n'),url=URL.createObjectURL(new Blob([body],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=s.month+'_'+s.variant+'_交易.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
el('audit-summary').textContent=`${D.scenarios.length} 组完整账户回放均已通过独立审计。对照共 ${base.trade_count} 笔，所有方案合计 ${D.scenarios.reduce((a,s)=>a+s.trades.length,0)} 笔成交路径逐笔核验；其中交易有重叠。`;
el('storage-note').textContent=`本轮数据与输出当前约 ${(D.local_bytes/1024**3).toFixed(2)} GiB，共享5 GiB本地限额；磁盘保留至少4 GiB空闲。`;
el('fingerprints').textContent='实验声明 SHA256：'+A.plan_sha256+'。完整逐组指纹保存在随报告同步的 optimization_report_data.json；回放工具与测试包含在复现快照中。';render();
</script></body></html>'''


if __name__ == "__main__":
    build()
