"""Opportunity follow-up: independent Decimal/path audit; never calls the execution or trailing helpers."""

import argparse
import csv
import gzip
import itertools
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from research.calendar import Calendar
from research.config import ResearchError
from research.data import file_sha256
from research.feature_cache import read_frames
from research.reporting import write_csv, write_json
from research.storage import SpaceBudget

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research_outputs/2026-09/trailing_exit_review"
PLAN = ROOT / "research_inputs/2026-09/trailing_exit_plan.json"
MINUTE = timedelta(minutes=1)


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def require(condition, message):
    if not condition:
        raise ResearchError(message)


def close(actual, expected):
    require(math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-7), f"算术不符：{actual} / {expected}")


def compare_final_state(actual, expected, tick):
    """Allow binary roundoff in prices, while checking flags and clocks exactly."""
    require(actual.keys() == expected.keys(), "最终追踪状态字段不符")
    prices = {"activation_price", "initial_hard_stop", "best_price", "stop_price",
              "breakeven_activation_price", "breakeven_price"}
    tolerance = min(float(tick) * 1e-8, 1e-8)
    for field, value in expected.items():
        if field in prices:
            require(type(actual[field]) in (int, float)
                    and math.isclose(actual[field], value, rel_tol=0, abs_tol=tolerance),
                    "最终追踪价格不符：" + field)
        else:
            require(actual[field] == value and type(actual[field]) is type(value),
                    "最终追踪状态不符：" + field)


def D(value):
    return Decimal(str(value))


def fee(rule, quantity, offset, price):
    cost = rule["fees"][offset]
    return D(cost["value"]) * quantity * (price * D(rule["value_per_price"]) if cost["mode"] == "rate" else 1)


def load_market(run, trades, cfg):
    needed = {(t["contract"], t["entry_time"][:10]) for t in trades}
    ref = read(run / "data_reference.json")
    source = (run / ref["object"]).resolve()
    require(file_sha256(source) == ref["sha256"], "原行情指纹不符")
    raw, frames, indices = {}, {}, {}
    with gzip.open(source, "rt") as stream:
        next(stream)
        for line in stream:
            item = json.loads(line)
            bar = item["row"]
            require(bar["trading_day"] <= cfg["splits"]["validation"]["end"], "行情包含锁定测试")
            key = bar["symbol"] + "." + bar["exchange"]
            if item["kind"] == "bar" and (key, bar["trading_day"]) in needed:
                raw[(key, bar["datetime"])] = bar
    evidence = read(run / "prepared_source_review.json")
    require(file_sha256(evidence["cache"]) == evidence["cache_sha256"], "指标来源指纹不符")
    for key, minutes, records in read_frames(evidence["cache"], evidence["cache_key"]):
        require(all(r["day"] <= cfg["splits"]["validation"]["end"] for r in records), "缓存包含锁定测试")
        if minutes != 1 or not any(k == key for k, _ in needed):
            continue
        frames[key] = records
        indices[key] = {r["end"]: i for i, r in enumerate(records)}
    return raw, frames, indices


def independent_path(trade, cfg, raw, records, index, events, meta):
    sign = 1 if trade["direction"] == "LONG" else -1
    key, opened = trade["contract"], datetime.fromisoformat(trade["entry_time"])
    last = datetime.fromisoformat(trade["exit_time"])
    hard, target, best = (D(trade[k]) for k in ("stop_price", "target_price", "entry_price"))
    stop, tick, active, armed, known = hard, D(meta["tick_size"]), False, None, trade["entry_time"]
    pending, signal_time, path, observations = [], None, [], []
    s, cal = cfg["strategy"], Calendar(cfg["calendar"])
    be = None
    if s.get("breakeven"):
        exit_fee = meta["fees"]["close_today"]
        value = D(meta["value_per_price"])
        opening_fee = D(trade["entry_fee"]) / int(trade["quantity"]) / value
        fixed = D(exit_fee["value"]) / value if exit_fee["mode"] == "fixed" else D(0)
        rate = D(exit_fee["value"]) if exit_fee["mode"] == "rate" else D(0)
        modeled = (best + sign * (opening_fee + fixed)) / (D(1) - sign * rate)
        raw_break_even = modeled + sign * s["slippage_ticks"] * tick
        price = (raw_break_even / tick).to_integral_value(rounding=ROUND_CEILING if sign > 0 else ROUND_FLOOR) * tick
        be = {"breakeven_active": False, "breakeven_armed_at": None,
              "breakeven_activation_price": float(best + sign * D(s["breakeven"]["activation_r"]) * abs(best-hard)),
              "breakeven_price": float(price), "breakeven_fee_basis": "entry_time_known_close_today",
              "breakeven_slippage_ticks": s["slippage_ticks"]}
    def stop_reason():
        if sign * (stop - hard) <= 0:
            return "fixed_stop"
        if be and be["breakeven_active"] and stop == D(be["breakeven_price"]):
            return "breakeven_stop"
        return "trailing_stop"
    _, force, session_end = cal.deadlines(opened.date().isoformat(), meta, s["times"])
    extrema = [best]
    now, outcome = opened, None
    while now <= last:
        timer = (["time_force"] if now >= force else []) + (["session_close"] if now >= session_end else [])
        if not s["allow_hold_across_break"] and cal.in_break(now + MINUTE, opened.date().isoformat(), meta):
            timer.append("break_close")
        if timer:
            signal_time = signal_time or now.isoformat()
            pending = list(dict.fromkeys(pending + timer))
        b = raw.get((key, now.isoformat()))
        if b is None:
            now += MINUTE
            continue
        o, h, low, c = (D(b[k]) for k in ("open", "high", "low", "close"))
        fillable = b.get("tradable", True) and b["volume"] > 0 and not (h == low and ((b.get("limit_up") is not None and h >= D(b["limit_up"])) or (b.get("limit_down") is not None and low <= D(b["limit_down"]))))
        step = {"start": now.isoformat(), "end": (now + MINUTE).isoformat(), "open": float(o), "high": float(h), "low": float(low), "close": float(c), "stop_at_open": float(stop), "known_at_open": known, "active_at_open": active, "exit_pending_at_open": bool(pending)}
        require(known <= now.isoformat(), "保护线使用未来信息")
        if pending and fillable:
            extrema.append(o)
            outcome = (now.isoformat(), o, pending, False, signal_time, "pending_next_open")
            path.append(step | {"exit": True})
            break
        gap = sign * (o - stop) <= 0
        hit = gap or (low <= stop if sign > 0 else h >= stop)
        reason = stop_reason()
        if hit:
            if fillable:
                price = o if gap else stop
                extrema.append(o)
                # The exit candle's other extreme may occur after the fill.
                outcome = (now.isoformat() if gap else (now + MINUTE).isoformat(), price, [reason], gap, signal_time or (now.isoformat() if gap else (now + MINUTE).isoformat()), "opening_gap" if gap else "intrabar_existing_line")
                path.append(step | {"exit": True, "exit_candle_extreme_excluded": True})
                break
            signal_time = signal_time or (now + MINUTE).isoformat()
            pending = list(dict.fromkeys(pending + [reason]))
        end = (now + MINUTE).isoformat()
        i = index.get(end)
        require(i is not None, "持仓期间缺少完成分钟指标")
        one = records[i]
        for field in ("open", "high", "low", "close", "volume"):
            close(one[field], b[field])
        flags = []
        weak = i >= 1 and sign * (one["close"] - one["open"]) < 0 and sign * (one["close"] - records[i - 1]["close"]) < 0
        if s["enable_volume_exit"] and one["vr"] is not None and one["vr"] >= s["volume_exit_multiple"] and (s["volume_exit_mode"] == "threshold" or weak):
            flags.append("volume")
        if s["enable_ma40_exit"] and one["ma40"] is not None:
            distance = sign * (one["close"] - one["ma40"])
            if distance <= 0:
                required = s.get("ma40_exit_confirmation_bars", 1)
                prior = records[i - 1] if i >= 1 else None
                confirmed = required == 1 or (
                    prior is not None and prior["ma40"] is not None
                    and datetime.fromisoformat(prior["end"]) == now
                    and datetime.fromisoformat(prior["end"]) >= opened + MINUTE
                    and sign * (prior["close"] - prior["ma40"]) <= 0
                )
                if confirmed:
                    flags.append("ma40_cross")
            elif s["ma40_mode"] == "approach" and one["previous_atr"] is not None and i >= 2 and distance <= s["ma40_approach_atr"] * one["previous_atr"] and all(sign * (records[j]["close"] - records[j - 1]["close"]) < 0 for j in (i - 1, i)):
                flags.append("ma40_approach")
        if not pending:
            diagnostic = {"entry_time": trade["entry_time"], "bar_end": end, "previous_stop": float(stop), "initial_hard_stop": float(hard), "activation_price": float(target), "close": float(c), "effective_from": "next_available_open", "close_exit_requested": False}
            if b["volume"] <= 0:
                diagnostic.update(available=False, reason="nonpositive_volume")
            else:
                best = max(best, h) if sign > 0 else min(best, low)
                if be:
                    if not be["breakeven_active"] and sign * (best - D(be["breakeven_activation_price"])) >= 0:
                        be["breakeven_active"], be["breakeven_armed_at"] = True, end
                    if be["breakeven_active"]:
                        stop = max(stop, D(be["breakeven_price"])) if sign > 0 else min(stop, D(be["breakeven_price"]))
                        known = end
                    diagnostic.update({key: be[key] for key in ("breakeven_active", "breakeven_armed_at", "breakeven_activation_price", "breakeven_price")})
                    diagnostic["close_exit_requested"] = sign * (c-stop) <= 0 and sign * (stop-hard) > 0
                if not active and sign * (best - target) >= 0:
                    active, armed = True, end
                atr = one["previous_atr"]
                diagnostic.update(best_price=float(best), active=active, atr_previous=atr)
                if atr is None or not math.isfinite(atr) or atr <= 0:
                    diagnostic.update(available=False, reason="invalid_atr", atr_previous=None, new_stop=float(stop))
                elif not active:
                    diagnostic.update(available=True, new_stop=float(stop), tightened=sign * (stop-D(diagnostic["previous_stop"])) > 0)
                else:
                    ticks = max(1, int((D(s["trailing_exit"]["atr_multiple"]) * D(atr) / tick).to_integral_value(rounding=ROUND_CEILING)))
                    candidate = ((best - sign * ticks * tick) / tick).to_integral_value(rounding=ROUND_FLOOR if sign > 0 else ROUND_CEILING) * tick
                    prior = D(diagnostic["previous_stop"])
                    stop = max(stop, hard, candidate) if sign > 0 else min(stop, hard, candidate)
                    known = end
                    breached = sign * (c - stop) <= 0 and sign * (stop - hard) > 0
                    diagnostic.update(available=True, distance_ticks=ticks, candidate_stop=float(candidate), new_stop=float(stop), tightened=sign * (stop - prior) > 0, close_exit_requested=breached)
                if diagnostic["close_exit_requested"]:
                    flags.append(stop_reason())
            observations.append(diagnostic)
            step.update(stop_after_close=float(stop), best_after_close=float(best), active_after_close=active, previous_atr=one["previous_atr"], vr=one["vr"], volume_weakness=weak, flags=flags)
        if b["volume"] > 0:
            extrema.extend([h, low])
        path.append(step)
        if flags:
            signal_time = signal_time or end
            pending = list(dict.fromkeys(pending + flags))
        now += MINUTE
    require(outcome is not None, "独立路径未能复现平仓")
    when, raw_exit, flags, gap, requested, kind = outcome
    precedence = ["time_force", "session_close", "break_close", "fixed_stop", "breakeven_stop", "trailing_stop", "fixed_target", "volume", "ma40_cross", "ma40_approach"]
    require(when == trade["exit_time"] and requested == trade["exit_signal_time"], "独立退出时间不符")
    require(flags == json.loads(trade["exit_flags"]) and next(f for f in precedence if f in flags) == trade["exit_reason"], "独立退出条件不符")
    require(str(gap) == trade["gap"], "跳空标记不符")
    close(trade["exit_price"], raw_exit - sign * s["slippage_ticks"] * tick)
    final_state = {"active": active, "activation_price": float(target), "initial_hard_stop": float(hard), "best_price": float(best), "stop_price": float(stop), "atr_multiple": s["trailing_exit"]["atr_multiple"], "armed_at": armed, "known_at": known}
    if be:
        final_state.update(be)
    compare_final_state(json.loads(trade["trailing_exit"]), final_state, tick)
    actual_events = [e for e in events if e["action"] == "trailing_observation" and e["contract"] == key and e["entry_time"] == trade["entry_time"]]
    require(len(actual_events) == len(observations), "追踪观察遗漏或多出")
    for actual, expected in zip(actual_events, observations, strict=True):
        require(actual["time"] == expected["bar_end"], "观察记录时间不符")
        for field, value in expected.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                close(actual[field], value)
            else:
                require(actual[field] == ("" if value is None else str(value)), "观察字段不符：" + field)
    favorable = max(extrema) if sign > 0 else min(extrema)
    adverse = min(extrema) if sign > 0 else max(extrema)
    qty_value = int(trade["quantity"]) * D(meta["value_per_price"])
    return {"id": int(trade["id"]), "contract": key, "entry_time": trade["entry_time"], "exit_time": when, "exit_kind": kind, "activated": active, "armed_at": armed, "breakeven_activated": be["breakeven_active"] if be else False, "final_stop": float(stop), "observations": len(observations), "line_tightenings": sum(r.get("tightened", False) for r in observations), "path": path, "verified_favorable_price": float(favorable), "verified_adverse_price": float(adverse), "favorable_gross_cny": float(max(D(0), sign * (favorable - D(trade["entry_price"])) * qty_value)), "giveback_to_raw_exit_cny": float(max(D(0), sign * (favorable - raw_exit) * qty_value)), "extrema_definition": "Completed held candles plus exit opening; other extrema of an intrabar exit candle excluded because sequence is unknown. Gross diagnostic, not achievable net profit."}


def audit_trades(run, cfg, signals):
    trades, events = list(rows(run / "trades.csv.gz")), list(rows(run / "events.csv.gz"))
    raw, frames, indices = load_market(run, trades, cfg)
    rules = {(r["trading_day"], r["contract"]): r for r in cfg["execution"]["qualification"]["rules"]}
    arithmetic, paths, total, fees_total, lots = [], [], D(0), D(0), Counter()
    for t in trades:
        key, day, qty = t["contract"], t["entry_time"][:10], int(t["quantity"])
        rule = rules[(day, key)]
        sign, tick, value = (1 if t["direction"] == "LONG" else -1), D(rule["tick_size"]), D(rule["value_per_price"])
        meta = max((m for m in cfg["metadata"]["contracts"] if m["symbol"] + "." + m["exchange"] == key and m.get("effective_from", "") <= day), key=lambda m: m.get("effective_from", "")) | rule
        entry, exit_price, hard, target = (D(t[k]) for k in ("entry_price", "exit_price", "stop_price", "target_price"))
        s, signal = cfg["strategy"], signals[(key, t["entry_signal_time"])]
        close(entry, D(raw[(key, t["entry_time"])]["open"]) + sign * tick * s["slippage_ticks"])
        require(all(json.loads(signal["filters"]).values()), "入场过滤未通过")
        require(json.loads(t["entry_snapshot"]) == json.loads(signal["snapshot"]), "入场快照不符")
        guard = json.loads(signal["fill_price_check"])
        require(guard["accepted"] and sign * (entry - D(guard["modeled_price_limit"])) <= tick * D("1e-8"), "入场超价格限制")
        initial = s["fixed_ticks"][t["product"]]
        protection, allocation = json.loads(t["entry_protection"]), json.loads(t["entry_allocation"])
        entry_cost = fee(rule, 1, "open", entry) + fee(rule, 1, "close_today", entry)
        atr = json.loads(t["entry_snapshot"])["atr_previous"]
        if s.get("entry_cost_filter"):
            actual = json.loads(t["entry_cost_check"])
            ratio = float(entry_cost / value + 2 * s["slippage_ticks"] * tick) / atr
            close(actual["cost_atr"], ratio)
            require(ratio <= s["entry_cost_filter"]["max_cost_atr"] + 1e-12 and actual["accepted"], "成交成本超出声明门槛")
        stop_ticks = max(initial["stop_loss_ticks"], math.ceil(atr * s["protection_scale"]["atr_multiple"] / float(tick) - 1e-9), math.ceil(s["protection_scale"]["roundtrip_cost_multiple"] * float(entry_cost / (tick * value) + 2 * s["slippage_ticks"]) - 1e-9), json.loads(signal["protection_plan"])["stop_loss_ticks"])
        require(stop_ticks == protection["stop_loss_ticks"], "初始保护尺度不符")
        close(hard, entry - sign * tick * stop_ticks)
        close(target, entry + sign * tick * math.ceil(stop_ticks * initial["take_profit_ticks"] / initial["stop_loss_ticks"] - 1e-9))
        planned = qty * (tick * stop_ticks * value + D(cfg["risk"]["cost_buffer_multiple"]) * (entry_cost + 2 * s["slippage_ticks"] * tick * value))
        close(planned, allocation["planned_risk"])
        require(qty <= allocation["reserved_quantity"] and planned <= D(allocation["single_trade_budget"]) + D("1e-6"), "手数或初始风险超额")
        lots[(day, key)] += qty
        require(qty >= rule.get("min_open_lots", 1) and (not rule.get("daily_open_limit") or lots[(day, key)] <= rule["daily_open_limit"]), "开仓限制不符")
        paths.append(independent_path(t, cfg, raw, frames[key], indices[key], events, meta))
        gross = sign * (exit_price - entry) * qty * value
        fees = fee(rule, qty, "open", entry) + fee(rule, qty, t["exit_offset"], exit_price)
        for name, expected in (("gross_pnl", gross), ("fees", fees), ("net_pnl", gross - fees)):
            close(t[name], expected)
        cal = Calendar(cfg["calendar"])
        close(t["holding_minutes"], cal.holding_minutes(datetime.fromisoformat(t["entry_time"]), datetime.fromisoformat(t["exit_time"]), meta))
        total, fees_total = total + gross - fees, fees_total + fees
        arithmetic.append({"id": t["id"], "contract": key, "net": str(gross - fees), "fees": str(fees), "planned_risk": str(planned), "passed": True})
    summary = read(run / "summary.json")
    close(summary["metrics"]["net_profit"], total)
    close(summary["metrics"]["fees"], fees_total)
    peak, drawdown = D(cfg["risk"]["initial_capital"]), D(0)
    risk_observations = 0
    for row in rows(run / "equity.csv.gz"):
        eq = D(row["equity"])
        peak, drawdown = max(peak, eq), max(drawdown, max(peak, eq) - eq)
        held = [t for t in trades if t["entry_time"] < row["time"] < t["exit_time"]]
        if held:
            reserved = sum(D(json.loads(t["entry_allocation"])["planned_risk"]) for t in held)
            require(D(row["risk"]) + D("1e-6") >= reserved, "追踪收紧后提前释放初始风险预算")
            risk_observations += 1
    close(summary["metrics"]["max_drawdown"], drawdown)
    require(not summary["open_positions"] and not summary["unflattened_risk"] and not summary["break_unflattened_risk"], "存在未平风险")
    return arithmetic, paths, str(total), str(fees_total), risk_observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["control", "trailing"])
    step = parser.parse_args().step
    plan = read(PLAN)
    parent, run = ROOT / plan["parent"], Path(read(OUT / (step + "_latest.json"))["directory"])
    cfg, old = read(run / "config_snapshot.json"), read(parent / "config_snapshot.json")
    require(read(run / "summary.json")["status"] == "completed", "回放未完成")
    require(cfg["development_review"]["plan_sha256"] == file_sha256(PLAN), "实验声明改变")
    budget = SpaceBudget(cfg["storage"]["budget"])
    for key in old:
        if key not in {"strategy", "development_review"}:
            require(cfg[key] == old[key], "非退出资料改变：" + key)
    require(cfg["strategy"] == (old["strategy"] if step == "control" else old["strategy"] | plan["strategy_delta"]), "策略改变超出声明")
    files = ("trades", "signals", "orders", "events", "equity", "daily_pool", "pool_exclusions", "daily_candidates", "candidate_execution", "rank_contribution") if step == "control" else ("daily_pool", "pool_exclusions", "daily_candidates", "candidate_execution")
    exact = {name: file_sha256(run / (name + ".csv.gz")) == file_sha256(parent / (name + ".csv.gz")) for name in files}
    require(all(exact.values()), "对照或原池排名不一致：" + str(exact))
    reuse = read(run / "prepared_entries_review.json")
    require(reuse["source_signals_sha256"] == file_sha256(parent / "signals.csv.gz"), "复用入场来源改变")
    result = {"status": "passed", "run": str(run), "parent": str(parent), "plan_sha256": file_sha256(PLAN), "exact_csv_matches": exact, "observations_replayed": reuse["observations_replayed"], "locked_test_read": False}
    if step == "trailing":
        count = changed_state = 0
        filled = {}
        for a, b in itertools.zip_longest(rows(parent / "signals.csv.gz"), rows(run / "signals.csv.gz")):
            require(a is not None and b is not None, "分钟观察行数变化")
            for field in ("time", "date", "contract", "direction", "rank", "r8", "group", "product", "snapshot", "pullback", "exit_flags", "execution_pass", "execution_rejections"):
                require(a[field] == b[field], "固有信号或执行资格改变：" + field)
            before, after = json.loads(a["filters"]), json.loads(b["filters"])
            changed_state += before.pop("state") != after.pop("state")
            require(before == after, "入场过滤条件改变")
            if b["filled"] == "True":
                filled[(b["contract"], b["time"])] = b
            count += 1
        require(count == reuse["observations_replayed"], "遗漏复用观察")
        arithmetic, paths, net, fees, risk_observations = audit_trades(run, cfg, filled)
        result.update(observations_checked=count, state_filter_changes=changed_state, intrinsic_entry_invariants=True, independent_path_and_fees=True, trade_count=len(arithmetic), net_profit=net, fees=fees, activated_trades=sum(p["activated"] for p in paths), trailing_observations=sum(p["observations"] for p in paths), initial_risk_reservation_observations=risk_observations)
        write_json(OUT / "trade_path_audit.json", paths, budget)
        write_csv(run / "independent_trade_arithmetic.csv", arithmetic, budget)
    write_json(OUT / (step + "_review.json"), result, budget)
    write_json(run / "independent_trailing_review.json", result, budget)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
