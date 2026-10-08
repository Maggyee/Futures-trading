"""Hypothetical rule-price paths. No fee, margin, cash or portfolio accounting.

The matching clock, causal signals, exits and cooldown come from the same engine.
Admission is one hypothetical unit per contract; account capacity is not modeled.
"""

import copy
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .calendar import MINUTE, stamp
from .config import ResearchError, digest, read_config
from .execution import PortfolioBacktest, can_fill, slipped
from .experiments import apply_calibration, code_identity
from .reporting import write_json
from .storage import SpaceBudget


def prepare_price_replay(config, calibration_path, output, index=0, execution_review=None):
    cfg = read_config(config)
    calibration = json.loads(Path(calibration_path).read_text())
    if (
        calibration.get("schema") != 2
        or calibration.get("locked_test_read") is not False
        or calibration["train_window"] != cfg["splits"]["train"]
    ):
        raise ResearchError("价格回放必须使用同一训练段v2候选，不读取验证或测试波动补算")
    cfg = apply_calibration(cfg, calibration, index)
    # A replay never activates or substitutes missing execution costs.
    cfg["execution"] = {"mode": "formal"}
    rules = []
    if execution_review:
        reviewed = read_config(execution_review)
        q = reviewed.get("execution", {}).get("qualification", {})
        if (
            reviewed["execution"].get("qualification_hash") != digest(q)
            or q.get("kind") != "DIAGNOSTIC_EXECUTION_ONLY"
            or q.get("calibration_hash") != digest(calibration)
            or q.get("fixed_ticks_hash") != digest(cfg["strategy"]["fixed_ticks"])
            or q.get("validation_window") != cfg["splits"]["validation"]
        ):
            raise ResearchError("可选报价步长证据必须来自同一版本的诊断复核")
        for row in q["rules"]:
            rules.append({k: row[k] for k in (
                "contract", "trading_day", "effective_from", "effective_to",
                "tick_size", "sources", "specification_available_at",
                "specification_effective_from", "specification_basis",
            )})
    cfg["price_replay"] = {
        "schema": 1,
        "kind": "HYPOTHETICAL_RULE_PRICE_REPLAY",
        "window": cfg["splits"]["validation"],
        "strategy_hash": digest(cfg["strategy"]),
        "basis_hash": digest({k: cfg[k] for k in ("metadata", "calendar")}),
        "calibration_hash": digest(calibration),
        "tick_rules": rules,
        "locked_test_read": False,
        "assumptions": [
            "假设成交的规则价格回放：每个合约最多一个假设单位，跟踪实际状态、退出及冷却；不模拟账户资金、组合风险预算或手数。",
            "沿用下一可成交分钟开盘、冻结的不利滑点及固定保护；有量分钟不保证盘口成交，未验证排队、部分成交及分钟内路径。",
            "原池、原排名、原Top-K不变；训练不足品种只在排名后拒绝，不补入下一名。",
            "供应商最新目录报价步长未经历史核实，仅作为固定保护和滑点的显式假设；无当时适用的已复核规范时不输出跳数盈亏。R值有条件地按冻结止损价格距离计算。",
            "缺成交额，沿用典型价量权近似均价；历史完整合约池和部分时段仍未核实。",
            "费用、账户净收益、资金收益率、保证金占用、夏普均不计算；未知成本没有记为0。候选索引仍为未确认研究初值。",
        ],
    }
    policy = cfg.get("storage", {}).get("budget")
    budget = SpaceBudget(policy) if policy else None
    output = Path(output).resolve()
    if budget:
        budget.check(output, reserve=8 * 1024**2)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "price_replay_config.json", cfg, budget)
    write_json(output / "preparation_manifest.json", {
        "source_configuration": str(Path(config).resolve()),
        "configuration_hash": digest(cfg),
        "calibration_path": str(Path(calibration_path).resolve()),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "locked_test_read": False,
        **code_identity(),
    }, budget)
    return {"config": str(output / "price_replay_config.json"), "tick_rules": len(rules)}


class PriceReplayParameters:
    mode = "price_replay"

    def __init__(self, cfg, metadata):
        self.cfg, self.metadata = cfg, metadata
        self.qualification = cfg.get("price_replay", {})
        q, calibration = self.qualification, cfg.get("calibration_snapshot", {})
        if (
            q.get("schema") != 1
            or q.get("kind") != "HYPOTHETICAL_RULE_PRICE_REPLAY"
            or q.get("locked_test_read") is not False
            or q.get("window") != cfg["splits"]["validation"]
            or q.get("strategy_hash") != digest(cfg["strategy"])
            or q.get("basis_hash") != digest({k: cfg[k] for k in ("metadata", "calendar")})
            or q.get("calibration_hash") != digest(calibration)
            or calibration.get("schema") != 2
            or calibration.get("locked_test_read") is not False
            or calibration.get("train_window") != cfg["splits"]["train"]
            or cfg["splits"]["validation"]["end"] >= cfg["splits"]["test"]["start"]
        ):
            raise ResearchError("价格回放缺少同一训练段、固定规则及报价依据快照；不能进入锁定测试")
        expected = apply_calibration(cfg, calibration, cfg["calibration_candidate_index"])
        if expected["strategy"]["fixed_ticks"] != cfg["strategy"]["fixed_ticks"]:
            raise ResearchError("价格保护与训练候选不一致")
        self.tick_rules = {}
        for row in q["tick_rules"]:
            start, end = stamp(row["effective_from"]), stamp(row["effective_to"])
            base = metadata.get(row["contract"], row["trading_day"])
            if (
                start >= end
                or not q["window"]["start"] <= row["trading_day"] <= q["window"]["end"]
                or start.date().isoformat() != row["trading_day"]
                or stamp(row["specification_available_at"]) > start
                or stamp(row["specification_effective_from"]) > start
                or not base
                or row["tick_size"] != base["tick_size"]
                or not row["sources"]
            ):
                raise ResearchError("价格回放的已复核报价步长证据错配或使用未来规范")
            self.tick_rules.setdefault((row["trading_day"], row["contract"]), []).append(row)

    def preflight(self, products, start, end, data=None):
        if {"start": start, "end": end} != self.qualification["window"]:
            raise ResearchError("价格回放只允许固定验证窗口，不能改边界或读取锁定测试")
        if data and data.until(self.cfg["splits"]["train"]["end"]).fingerprint != self.cfg["calibration_snapshot"]["train_data_hash"]:
            raise ResearchError("价格回放训练数据指纹已改变")

    def resolve(self, key, time):
        time = stamp(time)
        day = time.date().isoformat()
        base = self.metadata.get(key, day)
        if not base:
            return None, ["contract_metadata_missing"]
        product = base["product"]
        if product not in self.cfg["calibration_snapshot"]["products"]:
            return None, ["training_product_not_qualified"]
        ticks = self.cfg["strategy"]["fixed_ticks"].get(product, {})
        tick = base.get("tick_size")
        if not isinstance(tick, (float, int)) or not math.isfinite(tick) or tick <= 0:
            return None, ["price_step_missing"]
        if any(not isinstance(ticks.get(k), int) or ticks[k] <= 0 for k in ("stop_loss_ticks", "take_profit_ticks")):
            return None, ["frozen_protection_missing"]
        reviewed = next((r for r in self.tick_rules.get((day, key), []) if stamp(r["effective_from"]) <= time < stamp(r["effective_to"])), None)
        meta = copy.copy(base)
        meta["price_basis"] = {
            "assumed_tick_size": tick,
            "tick_basis_reviewed": reviewed is not None,
            "historical_tick_fully_verified": False,
            "basis": reviewed["specification_basis"] if reviewed else "supplier_current_snapshot_historical_applicability_assumed",
            "source": reviewed["sources"] if reviewed else base.get("source"),
            "source_asof": base.get("source_asof"),
            "calibration_candidate_index": self.cfg["calibration_candidate_index"],
        }
        return meta, []

    def candidate(self, row):
        meta, reasons = self.resolve(row["contract"], row["ranking_time"])
        return {**row, "price_basis_pass": meta is not None, "price_basis_rejections": reasons, "hypothetical": True}


@dataclass
class RulePosition:
    key: str
    meta: dict
    sign: int
    quantity: int
    price: float
    raw_price: float
    opened: object
    stop: float
    target: float
    signal: dict


class RulePriceReplay(PortfolioBacktest):
    def make_parameters(self):
        return PriceReplayParameters(self.cfg, self.data.metadata)

    def __init__(self, data, cfg=None, cache=None):
        super().__init__(data, cfg, cache)
        self.allocator, self.cash = None, None

    def equity_value(self):
        raise ResearchError("价格回放不计算账户权益")

    def record_account(self, time, ending, day):
        pass

    def event(self, time, key, action, **details):
        super().event(time, key, action, hypothetical=True, **details)

    def admit_opportunity(self, signal, meta, price, day, time):
        st = self.state(signal["contract"])
        blocked = (self.unflattened or self.break_risk) and self.cfg["risk"]["block_after_unflattened"]
        signal["risk_pass"] = not blocked
        signal["qualified_with_risk"] = not blocked
        if blocked:
            signal["risk_rejections"] = ["unflattened_previous_session"]
            st.previous_pass = False
            return
        st.name, st.pending = "ENTRY_PENDING", {"signal": signal}
        if signal["pullback"]:
            st.consumed.add(signal["pullback"]["event"])
        self.orders.append({"time": signal["time"], "contract": signal["contract"], "direction": signal["direction"], "rank": signal["rank"], "hypothetical": True, "units": 1})
        self.event(time + MINUTE, signal["contract"], "entry_requested", units=1, rank=signal["rank"])

    def fill_open(self, key, bar, time):
        st = self.state(key)
        ok, reason = can_fill(bar)
        if st.name in {"ENTRY_PENDING", "EXIT_PENDING"} and not ok:
            self.event(time, key, "fill_unavailable", state=st.name, reason=reason)
            return
        if st.name == "EXIT_PENDING":
            self.close(key, bar, bar.open, time, st.pending["flags"])
        elif st.name == "ENTRY_PENDING":
            meta, reasons = self.parameters.resolve(key, time)
            signal = st.pending["signal"]
            if reasons:
                self.event(time, key, "entry_cancelled", reason="fill_price_basis_recheck", rejections=reasons)
                st.name, st.pending, st.previous_pass = "FLAT", None, False
                return
            s = self.cfg["strategy"]
            sign = 1 if signal["direction"] == "LONG" else -1
            price = slipped(bar.open, sign, meta, s["slippage_ticks"])
            ticks = s["fixed_ticks"][meta["product"]]
            stop = round(price - sign * ticks["stop_loss_ticks"] * meta["tick_size"], 10)
            target = round(price + sign * ticks["take_profit_ticks"] * meta["tick_size"], 10)
            st.position = RulePosition(key, meta, sign, 1, price, bar.open, time, stop, target, signal)
            st.pending, st.name = None, "LONG" if sign > 0 else "SHORT"
            signal["filled"], signal["fill_time"] = True, time.isoformat()
            self.event(time, key, "entry_filled", price=price, units=1, stop=stop, target=target)

    def close(self, key, bar, raw_price, time, flags, ambiguous=False, gap=False):
        st, s = self.state(key), self.cfg["strategy"]
        p = st.position
        # The price basis is frozen at entry, including for a delayed exit.
        price = slipped(raw_price, -p.sign, p.meta, s["slippage_ticks"])
        distance = abs(p.price - p.stop)
        points = round(p.sign * (price - p.price), 10)
        precedence = ["time_force", "session_close", "break_close", "fixed_stop", "fixed_target", "volume", "ma40_cross", "ma40_approach"]
        basis = p.meta["price_basis"]
        self.trades.append({
            "id": len(self.trades) + 1, "hypothetical": True,
            "contract": key, "product": p.meta["product"], "group": p.meta["group"],
            "direction": "LONG" if p.sign > 0 else "SHORT", "units": 1,
            "entry_signal_time": p.signal["time"], "entry_time": p.opened.isoformat(),
            "exit_signal_time": st.pending["signal_time"] if st.name == "EXIT_PENDING" else time.isoformat(),
            "exit_time": time.isoformat(), "entry_price": p.price, "exit_price": price,
            "raw_entry_price": p.raw_price, "raw_exit_price": raw_price,
            "stop_price": p.stop, "target_price": p.target,
            "planned_stop_distance": distance, "price_points": points,
            "r_multiple": points / distance,
            "r_is_conditional_on_price_basis": True,
            "ticks_pnl": points / p.meta["tick_size"] if basis["tick_basis_reviewed"] else None,
            "price_basis": basis, "cost_status": "NOT_MODELED_UNKNOWN_COSTS_NOT_ZERO",
            "exit_reason": next((r for r in precedence if r in flags), flags[0]),
            "exit_flags": flags, "ambiguous_bar": ambiguous, "gap": gap,
            "holding_minutes": self.calendar.holding_minutes(p.opened, time, p.meta),
            "elapsed_minutes": (time - p.opened).total_seconds() / 60,
            "entry_mode": s["entry_mode"], "rank": p.signal["rank"], "r8": p.signal["r8"],
            "entry_snapshot": p.signal["snapshot"],
        })
        self.ambiguities += int(ambiguous)
        self.event(time, key, "exit_filled", price=price, units=1, flags=flags)
        st.position, st.pending = None, None
        st.name = "COOLDOWN" if s["cooldown_minutes"] else "FLAT"
        st.cooldown, st.closed_at = s["cooldown_minutes"], time
        return True

    def run(self, start, end):
        result = super().run(start, end)
        for row in result["signals"]:
            for old, new in (
                ("execution_pass", "price_basis_pass"), ("execution_rejections", "price_basis_rejections"),
                ("risk_pass", "state_admission_pass"), ("risk_rejections", "state_admission_rejections"),
                ("filled", "hypothetical_filled"),
            ):
                row[new] = row.pop(old)
            row.pop("qualified_with_risk")
            row["hypothetical"] = True
        result.pop("equity")
        result.pop("execution_qualification")
        result["unflattened_positions"] = result.pop("unflattened_risk")
        result["break_unflattened_positions"] = result.pop("break_unflattened_risk")
        result["provenance"] = "SYNTHETIC_TEST_ONLY" if self.cfg.get("synthetic") else "HYPOTHETICAL_RULE_PRICE_REPLAY"
        result["price_replay_assumptions"] = self.parameters.qualification["assumptions"]
        result["account_metrics_calculated"] = False
        result["locked_test_read"] = False
        result["path_summary"] = {
            "selected_candidate_records": sum(r["selected"] for r in self.candidates),
            "trigger_events": sum(r["trigger"] for r in self.signals),
            "hypothetical_requests": len(self.orders),
            "hypothetical_entries": sum(r["hypothetical_filled"] for r in self.signals),
            "closed_price_paths": len(self.trades),
            "exit_reasons": dict(Counter(t["exit_reason"] for t in self.trades)),
            "closed_paths_by_product": dict(Counter(t["product"] for t in self.trades)),
        }
        return result


def report_price_replay(directory, manifest, result, data=None):
    """The report intentionally has no account performance table."""
    directory = Path(directory)
    strategy = manifest["configuration"]["strategy"]
    lines = [
        "# K=2/direct 假设成交规则价格回放" if strategy["k"] == 2 else "# 假设成交规则价格回放",
        "", f"原池、原排名；K={strategy['k']}，entry_mode={strategy['entry_mode']}。窗口{result['start']}至{result['end']}。",
        "假设进出用于检查买卖规则和价格路径，不能作为账户收益、完整策略表现或最佳K结论。",
        "", f"状态{result['status']}；闭合价格路径{len(result['trades'])}条；仍持仓{len(result['open_positions'])}个。",
        "", *["- " + x for x in result["price_replay_assumptions"]],
        "", "## 可逐条复核的假设进出", "",
        "| 合约/方向 | 信号时间 | 假设入场 | 假设退出 | 进价 | 出价 | 价格点数 | 条件R | 退出原因 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for t in result["trades"]:
        lines.append(f"| {t['contract']} {t['direction']} | {t['entry_signal_time']} | {t['entry_time']} | {t['exit_time']} | {t['entry_price']} | {t['exit_price']} | {t['price_points']:.6g} | {t['r_multiple']:.4g} | {t['exit_reason']} |")
    lines += ["", "不同合约价格点数不相加为资金收益；R依赖假定报价步长下冻结的计划止损距离，未计入费用。负向越过止损和不利滑点可导致小于-1R。",
        "只在采用的已复核官方规范适用假设下输出ticks_pnl，其他路径该字段为空；未计算货币盈亏。",
        "", f"退出原因：{json.dumps(result['path_summary']['exit_reasons'], ensure_ascii=False)}。分钟双触及保守按止损优先：{result['ambiguous_trades']}条。",
        "零量、缺失、明确不可交易和已知封死涨跌停均不能假设成交；供应商缺少历史限价时不能识别所有封板，保留该限制。",
        "", f"行情指纹：{manifest['data_fingerprint']}；代码指纹：{manifest['code_hash']}。",]
    if data and result["trades"]:
        cases = replay_case_charts(directory, result, data)
        lines += ["", "案例固定取最早闭合路径以及其后最早另一退出原因，不按盈亏选择：", ""]
        lines += [f"- [{r['contract']} {r['exit_reason']}]({r['file']})" for r in cases]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory / "report.md"


def replay_case_charts(directory, result, data):
    import plotly.graph_objects as go

    chosen = []
    for trade in sorted(result["trades"], key=lambda t: (t["entry_time"], t["contract"])):
        if not chosen or trade["exit_reason"] != chosen[0]["exit_reason"]:
            chosen.append(trade)
        if len(chosen) == 2:
            break
    cases = []
    for trade in chosen:
        key, day = trade["contract"], trade["entry_time"][:10]
        rows = [b for b in data.by_contract[key] if b.trading_day == day]
        chart = go.Figure(go.Candlestick(x=[b.datetime for b in rows], open=[b.open for b in rows], high=[b.high for b in rows], low=[b.low for b in rows], close=[b.close for b in rows], name=key))
        for label, field in (("MA10", "ma10"), ("MA20", "ma20"), ("MA40", "ma40"), ("近似均价", "vwap")):
            signals = [s for s in result["signals"] if s["contract"] == key and s["date"] == day]
            chart.add_trace(go.Scatter(x=[s["time"] for s in signals], y=[s["snapshot"].get(field) for s in signals], name=label))
        chart.add_trace(go.Scatter(x=[trade["entry_time"], trade["exit_time"]], y=[trade["entry_price"], trade["exit_price"]], mode="markers+text", text=["假设入场", "假设退出 " + trade["exit_reason"]], name="假设进出"))
        chart.add_hline(y=trade["stop_price"], line_dash="dash", annotation_text="冻结止损")
        chart.add_hline(y=trade["target_price"], line_dash="dash", annotation_text="冻结止盈")
        chart.update_layout(title=f"假设成交价格路径 · {key} · {day}；未知成本未计入", template="plotly_white")
        name = f"case_path_{trade['id']:04d}.html"
        chart.write_html(Path(directory) / name, include_plotlyjs=True)
        cases.append({"trade_id": trade["id"], "contract": key, "exit_reason": trade["exit_reason"], "file": name})
    write_json(Path(directory) / "cases.json", cases)
    return cases
