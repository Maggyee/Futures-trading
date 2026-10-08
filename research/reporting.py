import csv
import gzip
import io
import json
from collections import defaultdict
from pathlib import Path

import numpy as np


def write_json(path, value, budget=None):
    if budget:
        from .storage import write_bounded_json

        write_bounded_json(path, value, budget)
        return
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def write_csv(path, rows, budget=None):
    fields = sorted({k for r in rows for k in r})
    path = Path(path)
    buffered = None
    if path.suffix == ".gz":
        from .storage import BoundedFile

        raw = path.open("wb")
        buffered = io.BufferedWriter(BoundedFile(raw, budget, path), buffer_size=1024 * 1024)
        zipped = gzip.GzipFile(
            filename="", mode="wb", mtime=0, fileobj=buffered
        )
        f = io.TextIOWrapper(zipped, encoding="utf-8-sig", newline="")
    else:
        if budget:
            from .storage import BoundedFile

            raw = path.open("wb")
            f = io.TextIOWrapper(
                BoundedFile(raw, budget, path), encoding="utf-8-sig", newline=""
            )
        else:
            raw = None
            f = path.open("w", encoding="utf-8-sig", newline="")
    try:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False, sort_keys=True)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in row.items()
                }
            )
    finally:
        try:
            f.close()
        finally:
            try:
                if buffered:
                    buffered.close()
            finally:
                if raw:
                    raw.close()


def quality_report(directory, data):
    from .storage import SpaceBudget

    policy = data.cfg.get("storage", {}).get("budget")
    if policy:
        SpaceBudget(policy).check(directory, reserve=1024 * 1024)
    quality = data.quality
    lines = [
        "# 真实数据与研究准备审计"
        if not data.cfg.get("synthetic")
        else "# SYNTHETIC_TEST_ONLY 工程数据审计",
        "",
        f"实际保留 {len(data.bars)} 根分钟。范围：{quality['scope']}。",
        "",
        "| 合约 | 品种 | 分钟数 | 开始 | 结束 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in quality["coverage"]:
        lines.append(
            f"| {row['contract']} | {row['product']} | {row['rows']} | {row['start']} | {row['end']} |"
        )
    lines += [
        "",
        f"数据指纹：`{data.fingerprint}`。校验错误 {len(quality['errors'])} 条；乱序等警告 {len(quality['warnings'])} 条。",
        "",
        "## 缺失与口径",
        "",
        "期望交易分钟未见记录时标为缺失，不前向填充；无法仅凭分钟文件判断无成交还是漏采。日历休息不计缺失。",
        "",
    ]
    for source in quality["sources"]:
        lines.append(
            f"- {Path(source['path']).name}：{json.dumps(source.get('counts', {}), ensure_ascii=False)}"
        )
    for row in quality["missing_minutes"]:
        if row["count"]:
            lines.append(
                f"- {row['date']} {row['contract']}：缺少 {row['count']} 个日盘分钟"
                + ("，整日无数据。" if row["entire_day_absent"] else "。")
            )
    lines += [
        "",
        "近似/代理：" + "；".join(quality["approximations"]),
        "夜盘没有 trading_day 的记录排除，不能按自然日补成交易日；原始来源及具体排除分钟见 data_quality.json。",
        "",
        "## 正式回测阻塞项",
        "",
    ]
    lines += ["- " + gap for gap in quality["configuration_gaps"]]
    if not quality["configuration_gaps"]:
        lines.append("未发现必填执行配置缺口；仍须审阅数据覆盖和交易日完整性。")
    lines += [
        "",
        "工程测试和合成样例仅核对计算。本次审计不是收益回测、不是样本外策略验证，也没有 tick/模拟交易/实盘验证。",
        "接入命令和模板说明见 research/README.md；缺少真实费用/日历时不生成伪造收益结论。",
    ]
    (Path(directory) / "report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def metrics(result, capital):
    if result.get("execution_mode") == "price_replay":
        from .config import ResearchError

        raise ResearchError("假设成交价格回放禁止计算账户收益、保证金或夏普")
    trades = result["trades"]
    equity = result["equity"]
    values = np.array([capital] + [r["equity"] for r in equity], dtype=float)
    peak = np.maximum.accumulate(values)
    drawdowns = values - peak
    max_dd = float(-drawdowns.min())
    net = float(values[-1] - capital)
    daily = {}
    for row in equity:
        daily[row["date"]] = row["equity"]
    daily_values = np.array([capital] + list(daily.values()))
    pnls = np.diff(daily_values)
    returns = pnls / daily_values[:-1] if len(daily) else np.array([])
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
    fees = sum(t["fees"] for t in trades)
    realized = sum(t["net_pnl"] for t in trades)
    enough = len(returns) >= 20 and len(trades) >= 20 and np.std(returns, ddof=1) > 0
    result_metrics = {
        "net_profit": net,
        "net_return": net / capital,
        "realized_trade_net": realized,
        "unrealized_and_open_fee_difference": net - realized,
        "max_drawdown": max_dd,
        "max_drawdown_fraction": float(np.max(-drawdowns / peak)),
        "return_drawdown_ratio": net / max_dd if max_dd else None,
        "daily_return_volatility": float(np.std(returns, ddof=1))
        if len(returns) > 1
        else None,
        "sharpe": float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(240))
        if enough
        else None,
        "sharpe_definition": "每日收市标记权益简单收益，样本标准差(ddof=1)，sqrt(240)年化；少于20日/20笔不显示",
        "trade_count": len(trades),
        "average_trade_net": realized / len(trades) if trades else None,
        "win_rate": len(wins) / len(trades) if trades else None,
        "payoff_ratio": float(np.mean(wins) / -np.mean(losses))
        if wins and losses
        else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "average_holding_minutes": float(
            np.mean([t["holding_minutes"] for t in trades])
        )
        if trades
        else None,
        "fees": fees,
        "fee_share_of_abs_gross": fees / sum(abs(t["gross_pnl"]) for t in trades)
        if any(t["gross_pnl"] for t in trades)
        else None,
        "slippage_cost_diagnostic": sum(t["slippage_cost_diagnostic"] for t in trades),
        "slippage_already_in_prices": True,
        "ambiguous_trades": result["ambiguous_trades"],
        "daily_count": len(daily),
        "sample_warning": "证据不足：需要更多独立交易日和样本外交易"
        if not enough
        else None,
        "candidate_count": sum(r["selected"] for r in result["daily_candidates"]),
        "risk_rejection_count": sum(
            r["trigger"] and r.get("execution_pass", True) and not r["risk_pass"]
            for r in result["signals"]
        ),
        "execution_rejection_count": sum(
            r["trigger"] and not r.get("execution_pass", True)
            for r in result["signals"]
        ),
        "candidate_execution_rejection_count": sum(
            r["selected"] and not r["execution_pass"]
            for r in result.get("candidate_execution", [])
        ),
        "mean_margin_utilization": float(
            np.mean([r["margin"] / capital for r in equity])
        )
        if equity
        else 0,
        "max_margin_utilization": max(
            (r["margin"] / capital for r in equity), default=0
        ),
        "unflattened_session_count": len(result["unflattened_risk"]),
        "unflattened_break_count": len(result.get("break_unflattened_risk", [])),
    }
    result_metrics["daily"] = [
        {"date": d, "equity": val, "net_pnl": float(pnl), "return": float(ret)}
        for (d, val), pnl, ret in zip(daily.items(), pnls, returns, strict=True)
    ]
    monthly = defaultdict(float)
    for row in result_metrics["daily"]:
        monthly[row["date"][:7]] += row["net_pnl"]
    result_metrics["monthly"] = dict(monthly)
    decomposition = {}
    for field in ("group", "product", "direction", "entry_mode", "exit_reason", "rank"):
        parts = defaultdict(list)
        for t in trades:
            parts[str(t[field])].append(t)
        decomposition[field] = {
            name: {
                "trade_count": len(items),
                "net_pnl": sum(t["net_pnl"] for t in items),
                "fees": sum(t["fees"] for t in items),
            }
            for name, items in sorted(parts.items())
        }
    decomposition["month"] = {}
    for month in sorted({t["exit_time"][:7] for t in trades}):
        items = [t for t in trades if t["exit_time"][:7] == month]
        decomposition["month"][month] = {
            "trade_count": len(items),
            "net_pnl": sum(t["net_pnl"] for t in items),
        }
    result_metrics["decomposition"] = decomposition
    return result_metrics


def funnel(signals):
    keys = [
        ("candidate", ["candidate"]),
        (
            "trend",
            [
                "warmup_1m",
                "warmup_higher",
                "current_session_15m",
                "trend_15m",
                "trend_5m",
                "trend_1m",
            ],
        ),
        ("vwap", ["vwap"]),
        ("oi", ["oi"]),
        ("smooth", ["efficiency", "shock", "extension"]),
    ]
    result = {name: 0 for name, _ in keys}
    result.update(entry_trigger=0, execution_pass=0, risk_pass=0, actual_fill=0)
    independent = defaultdict(int)
    for row in signals:
        for name, filters in keys:
            if name == "smooth" and "price_pattern_confirmed" in row["filters"]:
                filters = ["higher_trend_quality", "confirmation_window", "price_pattern_confirmed", "shock", "extension"]
            elif name == "smooth" and "higher_trend_quality" in row["filters"]:
                filters = ["higher_trend_quality", "trend_window_valid", "pullback_after_armed", "shock", "extension"]
            if not all(row["filters"][k] for k in filters):
                break
            result[name] += 1
        result["entry_trigger"] += bool(row["trigger"])
        result["execution_pass"] += bool(row["trigger"] and row.get("execution_pass", True))
        result["risk_pass"] += bool(row["risk_pass"])
        result["actual_fill"] += bool(row["filled"])
        for reason in (
            row["rejections"]
            + row["risk_rejections"]
            + row.get("execution_rejections", [])
        ):
            independent[reason] += 1
    return {
        "unit": "合约-已完成分钟观察；不是独立交易样本",
        "sequential": result,
        "independent_rejections": dict(independent),
    }


def rank_contribution(result):
    ranks = sorted({r["rank"] for r in result["daily_candidates"]})
    counts = defaultdict(lambda: defaultdict(int))
    for signal in result["signals"]:
        row = counts[signal["rank"]]
        row["qualified"] += all(v for k, v in signal["filters"].items() if k not in {"candidate", "state"})
        row["trigger"] += bool(signal["trigger"])
        row["execution"] += bool(signal["trigger"] and signal.get("execution_pass", True))
        row["execution_rejected"] += bool(signal["trigger"] and not signal.get("execution_pass", True))
        row["risk_rejected"] += bool(signal["trigger"] and signal.get("execution_pass", True) and not signal["risk_pass"])
    rows = []
    for rank in ranks:
        trades = [t for t in result["trades"] if t["rank"] == rank]
        count = counts[rank]
        rows.append(
            {
                "rank": rank,
                "qualified_minutes_without_candidate_or_state_gate": count["qualified"],
                "trigger_count": count["trigger"],
                "execution_qualified_triggers": count["execution"],
                "execution_rejections": count["execution_rejected"],
                "risk_rejections": count["risk_rejected"],
                "trade_count": len(trades),
                "net_pnl": sum(t["net_pnl"] for t in trades),
            }
        )
    return rows


def report_run(directory, data=None, result=None):
    from .storage import read_result, restore_dataset

    directory = Path(directory)
    if result is None:
        result = read_result(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if result.get("status") in {"failed", "blocked"}:
        text = f"# 离线期货研究报告\n\n实验 {manifest['experiment_id']}：未完成正式回测。\n\n原因：{result.get('error')}\n"
        (directory / "report.md").write_text(text)
        return directory / "report.md"
    cfg = manifest["configuration"]
    policy = cfg.get("storage", {}).get("budget")
    if policy:
        from .storage import SpaceBudget

        SpaceBudget(policy).check(directory, reserve=64 * 1024 * 1024)
    if data is None:
        snapshot = restore_dataset(directory, cfg)
        if snapshot is not None:
            data = snapshot
    elif data.fingerprint != manifest["data_fingerprint"]:
        raise ValueError("报告行情与实验指纹不匹配")
    elif (reference_path := directory / "data_reference.json").exists():
        from .config import ResearchError
        from .data import file_sha256

        reference = json.loads(reference_path.read_text())
        if (
            file_sha256((directory / reference["object"]).resolve())
            != reference["sha256"]
        ):
            raise ResearchError("共享行情 SHA256 不匹配，拒绝生成报告")
    if result.get("execution_mode") == "price_replay":
        from .price_replay import report_price_replay

        return report_price_replay(directory, manifest, result, data)
    stats = result["metrics"]
    label = (
        "工程测试样例（SYNTHETIC_TEST_ONLY），不能用于策略收益验证"
        if cfg.get("synthetic")
        else "诊断分钟回测（原排名、部分候选可执行；不能作为完整策略收益验证）"
        if result.get("execution_mode") == "diagnostic"
        else "分钟级历史回测"
    )
    lines = [
        "# 开盘强弱与多周期波段研究报告",
        "",
        f"实验编号：`{manifest['experiment_id']}`。层级：{label}。",
        f"窗口：{result['start']} 至 {result['end']}；用途：{manifest['split']}；资金场景：{manifest['scope']}。",
        f"实际基准：K={cfg['strategy']['k']}，entry_mode={cfg['strategy']['entry_mode']}；固定声明：{json.dumps(cfg.get('baseline_expectation'), ensure_ascii=False)}。",
        "",
        "## 实际覆盖与假设",
        "",
        f"数据指纹：`{manifest['data_fingerprint']}`；代码指纹：`{manifest['code_hash']}`。",
        f"实际合约：{', '.join(r['contract'] for r in manifest['data_coverage']) or '无'}。并非全市场排名。",
        f"VWAP：{'典型价近似均价' if cfg['strategy']['approximate_vwap'] else '成交额/成交量/合约换算系数'}。",
        "ATR 使用 TA-Lib Wilder 平滑；异常幅度/乖离使用前一根 ATR。开盘价格为首分钟 open；无开盘快照时 OI 使用首分钟末代理。",
        "时间戳标准为上海时区分钟开始；不补造可成交行情。撮合假设为下一可交易分钟开盘加不利滑点，分钟 OHLC 止损止盈近似，双触及保守止损优先。",
        "只使用上一完整交易日的真实合约数据；日盘合约当日锁定。分组预留比例："
        + json.dumps(cfg["risk"]["group_fractions"], ensure_ascii=False)
        + "，为研究配置。",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    if result.get("execution_mode") == "diagnostic":
        qualified = result.get("candidate_execution", [])
        selected = [r for r in qualified if r["selected"]]
        from collections import Counter

        rejected = Counter(
            reason for r in selected for reason in r["execution_rejections"]
        )
        lines[lines.index("## 结果") : lines.index("## 结果")] = [
            "## 诊断执行范围",
            "",
            f"原Top-K候选 {len(selected)} 个，执行资格通过 {sum(r['execution_pass'] for r in selected)} 个。其他候选保留原排名并拒绝，不补选；详见candidate_execution.csv.gz。",
            f"候选执行拒绝原因（各项独立，可重叠）：{json.dumps(dict(rejected), ensure_ascii=False)}。",
            "正式全市场回测仍受原配置缺口阻止。本结果仅说明此费用/成交假设下已执行候选的诊断表现，不是完整策略收益或最佳K结论。",
            "锁定测试收益未运行；诊断不能用于freeze或参数搜索。",
            "",
            *["- " + item for item in result["execution_qualification"]["assumptions"]],
            "",
        ]
    for key in (
        "net_profit",
        "net_return",
        "max_drawdown",
        "return_drawdown_ratio",
        "daily_return_volatility",
        "trade_count",
        "average_trade_net",
        "payoff_ratio",
        "average_holding_minutes",
        "fees",
        "fee_share_of_abs_gross",
        "sharpe",
        "ambiguous_trades",
        "execution_rejection_count",
        "candidate_execution_rejection_count",
    ):
        lines.append(f"| {key} | {stats.get(key)} |")
    lines.extend(
        [
            "",
            stats["sharpe_definition"],
            stats["sample_warning"] or "样本规模达到显示阈值，仍不能保证参数稳定。",
            f"未平仓风险记录 {len(result['unflattened_risk'])} 条；状态 {result['status']}。未平仓权益按最后可用价格标记，不算作已实现成交。",
            f"禁止跨休息持仓时的未平风险记录 {len(result.get('break_unflattened_risk', []))} 条；不能将发送平仓请求视为已清仓。",
            "",
            "## 信号和归因",
            "",
            f"漏斗：`{json.dumps(result['signal_funnel']['sequential'], ensure_ascii=False)}`。",
            "每项拒绝原因独立保存在 signals.csv/json；费用、成交、全部退出标记和排名贡献保存在对应文件。",
            "排名贡献是受当前组合额度约束的实际贡献；不同 K 的低排名成交差异可反映资金竞争，不能直接当作新增品种独立质量。",
            "",
            "## 验证边界",
            "",
            "工程验收以 tests/test_research*.py 的实际执行结果为准。",
            "验证窗口可用于开发；锁定测试仅允许预先冻结配置。当前没有 tick、模拟交易或实盘执行验证。",
            "本报告不宣布最佳参数。合成样例中的所有金额仅用于核对软件计算。",
            "",
            "图表：equity.html、drawdown.html、monthly.html；案例按最早盈利、最早亏损、最早未入场确定，缺少该类案例时不强行补造。",
        ]
    )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    import plotly.graph_objects as go

    charts = {
        "equity": go.Figure(
            go.Scatter(
                x=[r["time"] for r in result["equity"]],
                y=[r["equity"] for r in result["equity"]],
                name="权益",
            )
        ),
        "drawdown": go.Figure(
            go.Scatter(
                x=[r["time"] for r in result["equity"]],
                y=(
                    np.array([r["equity"] for r in result["equity"]])
                    - np.maximum.accumulate(
                        [cfg["risk"]["initial_capital"]]
                        + [r["equity"] for r in result["equity"]]
                    )[1:]
                ).tolist(),
                name="回撤",
            )
        ),
        "monthly": go.Figure(
            go.Bar(
                x=list(stats["monthly"]),
                y=list(stats["monthly"].values()),
                name="月度盈亏",
            )
        ),
    }
    for name, chart in charts.items():
        chart.update_layout(title=label + " · " + name, template="plotly_white")
        chart.write_html(directory / (name + ".html"), include_plotlyjs=True)
    if data is not None:
        case_charts(directory, result, data)
    return directory / "report.md"


def case_charts(directory, result, data):
    import plotly.graph_objects as go

    from .calendar import stamp
    from .signals import Features

    chosen = []
    for name, predicate in (
        ("profit", lambda t: t["net_pnl"] > 0),
        ("loss", lambda t: t["net_pnl"] < 0),
    ):
        trade = next(
            (
                t
                for t in sorted(
                    result["trades"], key=lambda t: (t["entry_time"], t["contract"])
                )
                if predicate(t)
            ),
            None,
        )
        if trade:
            chosen.append((name, trade["contract"], trade["entry_time"], trade))
    rejected = next((s for s in result["signals"] if s["rejections"]), None)
    if rejected:
        chosen.append(("rejected", rejected["contract"], rejected["time"], None))
    from .data import Dataset

    selected_keys = {key for _, key, _, _ in chosen}
    # Every selected contract keeps its own full warmup; no other contracts'
    # minute/indicator copies are needed to draw these deterministic cases.
    case_data = Dataset([b for b in data.bars if b.key in selected_keys], data.cfg)
    feature = Features(case_data)
    cases = []
    for name, key, when, trade in chosen:
        day = stamp(when).date().isoformat()
        rows = [
            b
            for b in data.by_contract[key]
            if b.trading_day == day
            and data.calendar.locate(b.datetime, day, data.metadata.get(key, day))
        ]
        chart = go.Figure(
            go.Candlestick(
                x=[b.datetime.isoformat() for b in rows],
                open=[b.open for b in rows],
                high=[b.high for b in rows],
                low=[b.low for b in rows],
                close=[b.close for b in rows],
                name=key,
            )
        )
        for m in (10, 20, 40):
            values = [feature.latest(key, 1, b.end)[f"ma{m}"] for b in rows]
            chart.add_trace(
                go.Scatter(
                    x=[b.datetime.isoformat() for b in rows], y=values, name=f"SMA{m}"
                )
            )
        sigs = [
            s for s in result["signals"] if s["contract"] == key and s["date"] == day
        ]
        from .calendar import MINUTE

        chart.add_trace(
            go.Scatter(
                x=[(stamp(s["time"]) - MINUTE).isoformat() for s in sigs],
                y=[s["snapshot"]["vwap"] for s in sigs],
                name="VWAP",
            )
        )
        if trade:
            chart.add_trace(
                go.Scatter(
                    x=[trade["entry_time"], trade["exit_time"]],
                    y=[trade["entry_price"], trade["exit_price"]],
                    mode="markers+text",
                    text=[trade["entry_mode"], trade["exit_reason"]],
                    name="成交",
                )
            )
        else:
            chart.add_annotation(
                x=when,
                y=rejected["snapshot"].get("ma20"),
                text=", ".join(rejected["rejections"]),
                showarrow=True,
            )
        chart.update_layout(
            title=f"{result['provenance']} · 可复现案例 {name}: {key}",
            template="plotly_white",
        )
        chart.write_html(directory / f"case_{name}.html", include_plotlyjs=True)
        cases.append(
            {
                "category": name,
                "contract": key,
                "time": when,
                "file": f"case_{name}.html",
            }
        )
    write_json(directory / "cases.json", cases)
