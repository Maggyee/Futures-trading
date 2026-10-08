"""Independent pool, channel, stop-reentry and complete protection audits."""

import argparse
import copy
import gzip
import json
import math
import os
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from . import coverage_audit
from .calendar import MINUTE
from .coverage_audit import rows
from .data import file_sha256
from .feature_cache import read_frames
from .optimization_audit import close, require
from .optimization_declaration import validate_optimization
from .reporting import write_json
from .storage import SpaceBudget


def selection_sources(run, cfg):
    needed = {(c["contract"], row["time"]) for row in rows(run / "candidate_selection.csv.gz")
              for c in json.loads(row["checks"]) if c["action"] != "retain_held_or_reserved"}
    reference = json.loads((run / "data_reference.json").read_text())
    source = (run / reference["object"]).resolve()
    require(file_sha256(source) == reference["sha256"], "补位原始行情指纹不符")
    cutoff = json.loads((run / "manifest.json").read_text())["window"]["end"]
    quotes, atrs = {}, {}
    with gzip.open(source, "rt") as stream:
        next(stream)
        for line in stream:
            record = json.loads(line)
            if record["kind"] != "bar":
                continue
            bar = record["row"]
            require(bar["trading_day"] <= cutoff, "补位审计读取了截止后的行情")
            key = bar["symbol"] + "." + bar["exchange"]
            end = (datetime.fromisoformat(bar["datetime"]) + MINUTE).isoformat()
            if (key, end) in needed:
                quotes[(key, end)] = bar["close"]
    evidence = json.loads((run / "prepared_source_review.json").read_text())
    require(file_sha256(evidence["cache"]) == evidence["cache_sha256"], "补位指标指纹不符")
    for key, minutes, records in read_frames(evidence["cache"], evidence["cache_key"]):
        if minutes == 1:
            for record in records:
                if (key, record["end"]) in needed:
                    atrs[(key, record["end"])] = record["previous_atr"]
    return quotes, atrs, reference["sha256"]


def audit_selection(run, cfg, fee_solver):
    pool = bool(cfg["strategy"].get("candidate_pool"))
    if not cfg["strategy"].get("candidate_replacement") and not pool:
        return {"status": "passed", "enabled": False}
    candidates = {(r["date"], r["contract"]): r for r in rows(run / "daily_candidates.csv.gz")}
    quotes, atrs, source_sha = selection_sources(run, cfg)
    rules = {}
    for rule in cfg["execution"]["qualification"]["rules"]:
        rules.setdefault((rule["trading_day"], rule["contract"]), []).append(rule)
    opened = list(rows(run / "trades.csv.gz"))
    risk = cfg["risk"]
    checked, substituted = 0, set()
    for row in rows(run / "candidate_selection.csv.gz"):
        checks, selected = json.loads(row["checks"]), set(json.loads(row["selected"]))
        held = {c["contract"] for c in checks if c["action"] == "retain_held_or_reserved"}
        require(len(selected) <= 2 and held <= selected, "补位超过K或挤出已有预占")
        remainder = [c for c in checks if c["action"] != "retain_held_or_reserved"]
        require([c["rank"] for c in remainder] == sorted(c["rank"] for c in remainder), "补位顺序未按原排名")
        expected, usage = set(held), json.loads(row["group_usage"])
        eq = Decimal(row["equity"])
        capital = min(Decimal(str(risk["initial_capital"])), eq)
        fraction = Decimal(str(risk["group_fractions"][row["group"]]))
        for c in checks:
            source = candidates[(row["date"], c["contract"])]
            require(int(source["rank"]) == c["rank"] and source["group"] == row["group"]
                    and source["direction"] == row["direction"], "补位改变了原排名/组/方向")
            if c["action"] == "retain_held_or_reserved":
                continue
            require(len(expected) < 2, "已有两个名额后继续扩展候选")
            identity = c["contract"], row["time"]
            applicable = [r for r in rules.get((row["date"], c["contract"]), [])
                          if r["effective_from"] <= row["time"] < r["effective_to"]
                          and r["available_at"] <= row["time"]]
            qualification = cfg["execution"]["qualification"]
            ready = (source["product"] in qualification["training_ready_products"]
                     and c["contract"].rsplit(".", 1)[1] in qualification["allowed_exchanges"])
            atr = atrs.get(identity)
            assessed = (identity in quotes and bool(applicable) and ready
                        and atr is not None and math.isfinite(atr) and atr > 0)
            require(bool(c["assessed"]) == assessed, "补位资料可用性与原始来源不符")
            if c["assessed"]:
                require(c["quote_time"] == row["time"], "资金预检使用未来行情")
                rule = applicable[-1]
                close(c["price"], quotes[identity], "补位使用已完成收盘价")
                close(c["atr_previous"], atr, "补位ATR前值")
                for field in ("tick_size", "value_per_price", "margin_rate"):
                    close(c[field], rule[field], "补位执行参数：" + field)
                meta = max((m for m in cfg["metadata"]["contracts"]
                            if m["symbol"] + "." + m["exchange"] == c["contract"]
                            and m.get("effective_from", "") <= row["date"]
                            and (not m.get("effective_to") or row["date"] <= m["effective_to"])),
                           key=lambda m: m.get("effective_from", ""))
                require(c["minimum_open_lots"] == rule.get("min_open_lots", meta.get("min_open_lots", 1)), "补位最小手数不符")
                fees = fee_solver(rule, 1, "open", Decimal(str(quotes[identity]))) + fee_solver(rule, 1, "close_today", Decimal(str(quotes[identity])))
                close(c["roundtrip_fees_per_lot"], fees, "补位费用独立计算")
                strategy, scale = cfg["strategy"], cfg["strategy"]["protection_scale"]
                tick, value = Decimal(str(rule["tick_size"])), Decimal(str(rule["value_per_price"]))
                cost_ticks = float(fees / (tick * value)) + 2 * strategy["slippage_ticks"]
                stop = max(strategy["fixed_ticks"][source["product"]]["stop_loss_ticks"],
                           math.ceil(scale["atr_multiple"] * atr / float(tick) - 1e-9),
                           math.ceil(scale["roundtrip_cost_multiple"] * cost_ticks - 1e-9))
                require(c["stop_loss_ticks"] == stop, "补位止损风险距离不符")
                limit = rule.get("daily_open_limit", meta.get("daily_open_limit"))
                remaining = None if limit is None else max(0, limit - sum(int(t["quantity"]) for t in opened
                    if t["contract"] == c["contract"] and t["entry_time"][:10] == row["date"] and t["entry_time"] < row["time"]))
                require(c["daily_open_remaining"] == remaining, "补位当日开仓余量不符")
                per_risk = Decimal(str(c["stop_loss_ticks"])) * Decimal(str(c["tick_size"])) * Decimal(str(c["value_per_price"]))
                costs = Decimal(str(c["roundtrip_fees_per_lot"])) + 2 * cfg["strategy"]["slippage_ticks"] * Decimal(str(c["tick_size"])) * Decimal(str(c["value_per_price"]))
                per_risk += Decimal(str(risk["cost_buffer_multiple"])) * costs
                per_margin = Decimal(str(c["price"])) * Decimal(str(c["value_per_price"])) * Decimal(str(c["margin_rate"]))
                amounts = {
                    "single_trade_risk": capital * Decimal(str(risk["trade_risk_fraction"])),
                    "portfolio_risk": capital * Decimal(str(risk["portfolio_risk_fraction"])) - Decimal(row["risk_used"]),
                    "group_risk": capital * Decimal(str(risk["portfolio_risk_fraction"])) * fraction - Decimal(str(usage["risk"])),
                    "margin": eq * Decimal(str(risk["margin_fraction"])) - Decimal(row["margin_used"]),
                    "group_margin": eq * Decimal(str(risk["margin_fraction"])) * fraction - Decimal(str(usage["margin"])),
                }
                caps = {k: int(max(Decimal(0), v) / (per_margin if k in ("margin", "group_margin") else per_risk)) for k, v in amounts.items()}
                caps["max_lots"] = risk["max_lots_per_contract"]
                caps["max_positions"] = risk["max_lots_per_contract"] if int(row["slots_used"]) < risk["max_positions"] else 0
                if c["daily_open_remaining"] is not None:
                    caps["daily_open_limit"] = c["daily_open_remaining"]
                qty = max(0, min(caps.values()))
                if 0 < qty < c["minimum_open_lots"]:
                    qty = 0
                require(qty == c["quantity"], "独立容量计算不符")
                if pool:
                    ratio = (float(fees) / rule["value_per_price"]
                             + 2 * strategy["slippage_ticks"] * rule["tick_size"]) / atr
                    accepted = ratio <= strategy["entry_cost_filter"]["max_cost_atr"] + 1e-12
                    close(c["cost_atr"], ratio, "候选池成本ATR")
                    require(c["accepted_cost"] == accepted, "候选池成本门槛不符")
                    expected_action = ("skip_zero_capacity" if qty == 0 else
                                       "select" if accepted else "skip_cost")
                    require(c["action"] == expected_action, "可成交候选池跳过原因不符")
                else:
                    require((c["action"] == "skip_zero_capacity") == (qty == 0), "跳过了可承担合约")
                close(c["planned_risk"], qty * per_risk, "补位计划风险")
                close(c["margin"], qty * per_margin, "补位保证金")
            else:
                require(c["action"] == ("skip_unavailable" if pool else "select"), "未知执行资料处理不符")
            if c["action"] == "select":
                expected.add(c["contract"])
                if c["rank"] > 2:
                    substituted.add((row["date"], c["contract"], row["direction"]))
            checked += 1
        require(expected == selected, "实际补位未匹配顺序前缀")
    return {"status": "passed", "enabled": True, "capacity_checks": checked,
            "substituted_contract_day_directions": len(substituted),
            "quotes_atr_fees_execution_inputs_checked_against_sources": True,
            "source_data_sha256": source_sha}


def audit_journal(run, cfg, fee_solver):
    plan, _ = validate_optimization(cfg)
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    original = iter(rows(parent / "signals.csv.gz"))
    peer = next(original, None)
    rules = {(r["trading_day"], r["contract"]): r for r in cfg["execution"]["qualification"]["rules"]}
    ranks = {(r["date"], r["contract"]): r for r in rows(Path(run) / "daily_candidates.csv.gz")}
    dual = bool(cfg["strategy"].get("dual_entry"))
    five = {}
    if dual:
        evidence = json.loads((Path(run) / "prepared_source_review.json").read_text())
        for key, minutes, records in read_frames(evidence["cache"], evidence["cache_key"]):
            if minutes == 5:
                five[key] = ([r["end"] for r in records], records)
    counts, consumed = Counter(), set()
    stops = {}
    for trade in rows(Path(run) / "trades.csv.gz"):
        if trade["exit_reason"] == "fixed_stop" and float(trade["net_pnl"]) < 0:
            identity = trade["exit_time"][:10], trade["contract"], trade["direction"]
            stops.setdefault(identity, []).append(trade["exit_time"])
    removed = {"efficiency", "trend_activity", "trend_displacement",
               "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"}
    for row in rows(Path(run) / "signals.csv.gz"):
        flags, snapshot = json.loads(row["filters"]), json.loads(row["snapshot"])
        trend = dual and snapshot.get("entry_channel") == "pullback"
        require(not dual or snapshot.get("entry_channel") in {"direct", "pullback"}, "未知实际入场通道")
        require(row["all_pass"] == str(all(flags.values())), "总通过标记不符")
        require(set(json.loads(row["rejections"])) == {k for k, v in flags.items() if not v}, "拒绝原因不符")
        rank = ranks[(row["date"], row["contract"])]
        require(all(rank[k] == row[k] for k in ("rank", "direction", "r8", "group", "product")), "观察改变原候选身份")
        require(flags["candidate"] is True, "未选候选被记录为可入场")
        target = row["time"], row["contract"]
        while peer is not None and (peer["time"], peer["contract"]) < target:
            peer = next(original, None)
        if peer is not None and (peer["time"], peer["contract"]) == target:
            baseline = json.loads(peer["snapshot"])
            actual = {k: v for k, v in snapshot.items() if k not in {"trend_entry", "entry_channel"}}
            require(actual == baseline, "基础测量值改变")
            before = {k: v for k, v in json.loads(peer["filters"]).items() if k not in {"state", "cost", "stop_reentry"} | (removed if trend else set())}
            after = {k: v for k, v in flags.items() if k not in {"state", "cost", "stop_reentry", "higher_trend_quality", "trend_window_valid", "pullback_after_armed"}}
            require(before == after and row["exit_flags"] == peer["exit_flags"], "未声明的入场过滤改变")
            counts["unchanged_measurements"] += 1
        else:
            require(cfg["strategy"].get("candidate_pool") and int(row["rank"]) > 2, "出现未声明的新候选观察")
            counts["new_rank_measurements"] += 1
        if row["execution_pass"] == "True":
            check = json.loads(row["cost_check"])
            rule = rules[(row["date"], row["contract"])]
            price, atr = Decimal(str(check["price"])), snapshot["atr_previous"]
            fees = fee_solver(rule, 1, "open", price) + fee_solver(rule, 1, "close_today", price)
            distance = float(fees) / rule["value_per_price"] + 2 * cfg["strategy"]["slippage_ticks"] * rule["tick_size"]
            ratio = distance / atr if atr is not None and math.isfinite(atr) and atr > 0 else None
            accepted = ratio is not None and ratio <= cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-12
            require(accepted == flags["cost"] == check["accepted"], "成本过滤不符")
            if ratio is not None:
                close(check["cost_atr"], ratio, "独立成本ATR")
            counts["cost_checks"] += 1
        else:
            require(flags["cost"] is False, "无执行资料仍通过成本过滤")
        identity = row["date"], row["contract"], row["direction"]
        allowed = not any(t <= row["time"] for t in stops.get(identity, []))
        require(flags["stop_reentry"] == allowed, "止损后禁入标记不符")
        if not allowed:
            require(row["trigger"] == row["filled"] == "False", "补充通道绕过止损后禁入")
            counts["blocked_stop_reentries"] += 1
        if trend:
            context = snapshot["trend_entry"]
            times, records = five[row["contract"]]
            i = bisect_right(times, row["time"]) - 1
            require(i >= 0 and context["source_5m_end"] == times[i], "趋势使用未完成5分钟根")
            source = records[i]
            sign = 1 if row["direction"] == "LONG" else -1
            if i >= 10 and source["efficiency"] is not None:
                closes = [r["close"] for r in records[i-10:i+1]]
                path = sum(abs(b-a) for a, b in zip(closes[:-1], closes[1:], strict=True))
                expected = sign * (closes[-1] - closes[0]) / path if path > 0 else 0
                close(context["efficiency"], expected, "5分钟方向效率")
                quality = context["trend_quality"]
                if quality.get("observations") == 11 and source["previous_atr"] is not None:
                    tick = next(m["tick_size"] for m in cfg["metadata"]["contracts"] if m["symbol"]+"."+m["exchange"] == row["contract"])
                    changes = sum(abs(b-a) > tick*1e-8 for a,b in zip(closes[:-1],closes[1:],strict=True))
                    close(quality["signed_move_ticks"], sign * (closes[-1]-closes[0])/tick, "5分钟位移")
                    require(quality["nonzero_price_changes"] == changes, "5分钟活跃度不符")
                    require(context["filters"]["efficiency"] == (expected >= .45), "5分钟效率门槛不符")
                    rule = cfg["strategy"]["trend_quality"]
                    threshold = max(rule["min_displacement_atr"] * source["previous_atr"], rule["min_displacement_ticks"] * tick)
                    require(context["filters"]["trend_activity"] == (changes >= rule["min_price_changes"])
                            and context["filters"]["trend_displacement"] == (sign*(closes[-1]-closes[0]) >= threshold-tick*1e-8), "5分钟质量门槛不符")
            require(flags["higher_trend_quality"] == all(context["filters"].values()), "高周期资格不符")
            require(context["source_15m_end"] is None or context["source_15m_end"] <= row["time"], "15分钟使用未来根")
            clock = datetime.fromisoformat(row["time"])
            armed = datetime.fromisoformat(context["armed_at"]) if context["armed_at"] else None
            expiry = datetime.fromisoformat(context["expires_at"]) if context["expires_at"] else None
            opening = datetime.fromisoformat(context["period_open"]) if context["period_open"] else None
            valid = bool(armed and armed >= opening and armed <= clock < expiry and expiry == armed + 5*MINUTE)
            require(valid == flags["trend_window_valid"], "观察窗口跨时段或过期")
            touch = json.loads(row["pullback"]) if row["pullback"] else None
            after = bool(valid and touch and armed <= datetime.fromisoformat(touch["event"]) < clock)
            require(after == flags["pullback_after_armed"], "回踩未发生在资格启动之后")
            if row["trigger"] == row["risk_pass"] == "True":
                identity = row["date"], row["contract"], touch["event"]
                require(identity not in consumed, "同一回踩被重复发出开仓请求")
                consumed.add(identity)
            counts["higher_window_checks"] += 1
        counts["observations"] += 1
    return {"status": "passed", **dict(counts), "future_measurements_used": False}


def audit_channels(run, cfg):
    if not cfg["strategy"].get("dual_entry"):
        return {"status": "passed", "enabled": False}
    signals = {(row["contract"], row["time"]): row for row in rows(run / "signals.csv.gz")
               if row["all_pass"] == "True" or row["trigger"] == "True"}
    records = list(rows(run / "entry_channels.csv.gz"))
    require({(r["contract"], r["time"]) for r in records} == set(signals), "通道记录有遗漏或多出")
    checked, used, consumed = Counter(), set(), set()
    for record in records:
        identity = record["contract"], record["time"]
        require(identity not in used, "同一合约分钟存在重复通道决策")
        used.add(identity)
        row = signals[identity]
        common = json.loads(record["common_filters"])
        direct, pullback = (json.loads(record[k + "_filters"]) for k in ("direct", "pullback"))
        passing = {"direct": all(direct.values()) and all(common.values()),
                   "pullback": all(pullback.values()) and all(common.values())}
        require(all((record[k + "_pass"] == "True") == passing[k] for k in passing), "通道资格不符")
        event = json.loads(record["pullback"]) if record["pullback"] else None
        touch_identity = row["date"], row["contract"], event["event"] if event else None
        already_consumed = bool(event and touch_identity in consumed)
        require((record["pullback_consumed"] == "True") == already_consumed, "回踩消费状态不符")
        triggers = {"direct": passing["direct"] and record["previous_direct_pass"] == "False",
                    "pullback": passing["pullback"] and event is not None and not already_consumed}
        require(all((record[k + "_trigger"] == "True") == triggers[k] for k in triggers), "通道触发不符")
        chosen = ("direct" if triggers["direct"] else "pullback" if triggers["pullback"] else
                  "direct" if passing["direct"] else "pullback" if passing["pullback"] else "direct")
        require(record["chosen"] == chosen == json.loads(row["snapshot"])["entry_channel"], "同分钟未优先突破")
        require((row["trigger"] == "True") == triggers[chosen], "实际开仓触发与通道不符")
        require(json.loads(row["filters"]) == (direct if chosen == "direct" else pullback) | common,
                "实际入场过滤未匹配所选通道")
        if chosen == "pullback" and row["trigger"] == row["risk_pass"] == "True":
            consumed.add(touch_identity)
        checked[chosen + "_decisions"] += 1
        checked[chosen + "_fills"] += row["filled"] == "True"
    admitted = {}
    for order in rows(run / "orders.csv.gz"):
        signal = signals[(order["contract"], order["time"])]
        priority = (json.loads(signal["snapshot"])["entry_channel"] != "direct",
                    int(signal["rank"]), -abs(float(signal["r8"])), signal["group"], signal["contract"])
        admitted.setdefault(order["time"], []).append(priority)
    require(all(priorities == sorted(priorities) for priorities in admitted.values()),
            "同分钟未优先给突破请求分配资金")
    return {"status": "passed", "enabled": True, **dict(checked),
            "same_minute_priority_and_shared_gates_checked": True,
            "breakout_priority_in_shared_capital_admission": True}


def audit(directory):
    run = Path(directory)
    cfg = json.loads((run / "config_snapshot.json").read_text())
    plan, baseline = validate_optimization(cfg)
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    original_helper, original_pullbacks = coverage_audit.helper, coverage_audit.audit_pullbacks

    def helper(name):
        module, source = original_helper(name)
        if name == "audit_slope_band" and cfg["strategy"].get("dual_entry"):
            original_audit = module.audit_slope

            def channel_slope(signal, frames, configuration, metadata):
                actual = copy.deepcopy(configuration)
                if json.loads(signal["snapshot"]).get("entry_channel") == "pullback":
                    actual["strategy"]["slope_band"]["timeframes"] = {
                        "5m": configuration["strategy"]["slope_band"]["timeframes"]["5m"]}
                return original_audit(signal, frames, actual, metadata)
            module.audit_slope = channel_slope
        return module, source

    def channel_pullbacks(configuration, trades, signals, frames, indices):
        if not configuration["strategy"].get("dual_entry"):
            return original_pullbacks(configuration, trades, signals, frames, indices)
        selected = [t for t in trades if json.loads(signals[(t["contract"], t["entry_signal_time"])]["snapshot"])
                    .get("entry_channel") == "pullback"]
        actual = copy.deepcopy(configuration)
        actual["strategy"]["entry_mode"] = "pullback_ma10"
        return original_pullbacks(actual, selected, signals, frames, indices)

    with patch.object(coverage_audit, "audit_journal", audit_journal), \
            patch.object(coverage_audit, "helper", helper), \
            patch.object(coverage_audit, "audit_pullbacks", channel_pullbacks):
        result = coverage_audit.audit_run(run)
    trailing, _ = helper("audit_trailing_exit")
    result["candidate_selection"] = audit_selection(run, cfg, trailing.fee)
    result["entry_channels"] = audit_channels(run, cfg)
    names = ["daily_pool", "pool_exclusions", "daily_candidates", "candidate_execution"]
    if cfg["optimization_review"]["variant"] == "control":
        names += ["trades", "orders", "signals", "events", "equity", "rank_contribution"]
    exact = {n: file_sha256(run / (n + ".csv.gz")) == file_sha256(parent / (n + ".csv.gz")) for n in names}
    require(all(exact.values()), "原池、排名或对照记录不符：" + str(exact))
    require(cfg["strategy"]["breakeven"] == baseline["strategy"]["breakeven"] ==
            {"activation_r": 1.0, "include_costs": True}, "原1R保本改变")
    result["exact_csv_matches"] = exact
    result["protection_stage"] = {"status": "passed", "breakeven_r": 1,
                                   "independent_hard_stop_and_trailing_paths": True,
                                   "slope_checked_per_actual_entry_channel": True,
                                   "shared_fees_slippage_and_risk": True}
    write_json(run / "independent_ordered_opportunity_audit.json", result, SpaceBudget(plan["budget"]))
    links = []
    for path in run.glob("*.csv.gz"):
        source = parent / path.name
        if not source.exists():
            continue
        before, original = path.stat(), source.stat()
        if ((before.st_mode, before.st_uid, before.st_gid, before.st_size) !=
                (original.st_mode, original.st_uid, original.st_gid, original.st_size)):
            continue
        checksum = file_sha256(source)
        if file_sha256(path) != checksum or before.st_ino == original.st_ino:
            continue
        temporary = path.with_suffix(path.suffix + ".link.partial")
        os.link(source, temporary)
        os.replace(temporary, path)
        require(file_sha256(path) == checksum, "不可变记录去重校验失败")
        links.append({"path": str(path), "source": str(source), "sha256": checksum,
                      "released_bytes": before.st_size})
    write_json(run / "immutable_deduplication.json", {"verified_links": links,
                "released_bytes": sum(r["released_bytes"] for r in links)}, SpaceBudget(plan["budget"]))
    print(json.dumps({"status": "passed", "directory": str(run), "net": result["net"],
                      "trades": len(result["trades"]), "selection": result["candidate_selection"],
                      "channels": result["entry_channels"]}, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    audit(parser.parse_args().directory)
