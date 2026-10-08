"""Independent raw-bar geometry, confirmation state and complete exit-path audit.

The production pattern/stop helpers are deliberately not imported here. Existing
fee/path verification is reused only after reconstructing the structural floor
from raw completed bars, not from the reported stop price.
"""

import argparse
import copy
import gzip
import itertools
import json
import math
import os
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from . import coverage_audit
from .calendar import Calendar, MINUTE
from .coverage_audit import rows
from .data import Metadata, file_sha256
from .feature_cache import read_frames
from .optimization_audit import close, require
from .optimization_declaration import validate_optimization
from .reporting import write_json
from .storage import SpaceBudget

REMOVED = {"efficiency", "trend_activity", "trend_displacement",
           "slope_1m_ready", "slope_1m_minimum", "slope_1m_maximum"}
WANTED = ("warmup_higher", "current_session_15m", "trend_15m", "trend_5m",
          "slope_5m_ready", "slope_5m_minimum", "slope_5m_maximum")


def equivalent(actual, expected, label):
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), label + " 字段不符")
        for k in expected:
            equivalent(actual[k], expected[k], label + "/" + k)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), label + " 行数不符")
        for a, b in zip(actual, expected, strict=True):
            equivalent(a, b, label)
    elif type(expected) is float:
        close(actual, expected, label)
    else:
        require(actual == expected, label + " 不符：" + repr((actual, expected)))


def indicators(records, period):
    """Wilder recursion and direct close means, independently of Features/TA-Lib."""
    closes, trs, atr = [], [], None
    for i, row in enumerate(records):
        row["previous_atr"] = atr
        previous = closes[-1] if closes else None
        tr = max(row["high"] - row["low"], abs(row["high"]-previous), abs(row["low"]-previous)) if previous is not None else None
        if tr is not None:
            trs.append(tr)
        if i == period:
            atr = sum(trs) / period
        elif i > period:
            atr = ((period-1)*atr + tr) / period
        closes.append(row["close"])
        for n in (10, 20, 40):
            row["ma"+str(n)] = sum(closes[-n:]) / n if len(closes) >= n else None
        path = sum(abs(b-a) for a, b in zip(closes[-11:-1], closes[-10:], strict=True)) if i >= 10 else 0
        row["efficiency"] = (closes[-1]-closes[-11])/path if path else None
        row["ma20_slope3"] = row["ma20"]-records[i-3]["ma20"] if i >= 3 and row["ma20"] is not None and records[i-3]["ma20"] is not None else None


class Evidence:
    def __init__(self, run, cfg):
        self.cfg, self.calendar, self.metadata = cfg, Calendar(cfg["calendar"]), Metadata(cfg["metadata"])
        candidates = list(rows(run / "daily_candidates.csv.gz"))
        selected = {(r["contract"], r["date"]) for r in candidates if r["selected"] == "True"}
        keys = {k for k, _ in selected}
        evidence = json.loads((run / "prepared_source_review.json").read_text())
        source_run = Path(evidence["source_run"]) if evidence.get("shared_source_sha256") else run
        # September matching retains just forty raw warmup minutes per
        # contract. Rebuild its rolling higher indicators from the verified
        # complete original source, as the unchanged coverage audit also does.
        ref = json.loads((source_run / "data_reference.json").read_text())
        source = (source_run / ref["object"]).resolve()
        if evidence.get("shared_source_sha256"):
            require(ref["sha256"]==evidence["shared_source_sha256"], "完整指标预热来源不符")
        require(file_sha256(source) == ref["sha256"], "新规则的原始行情指纹不符")
        self.source_sha = ref["sha256"]
        cutoff = json.loads((run / "manifest.json").read_text())["window"]["end"]
        self.raw, buckets, periods = {}, defaultdict(list), {}
        with gzip.open(source, "rt") as stream:
            next(stream)
            for line in stream:
                item = json.loads(line)
                r = item["row"]
                require(r["trading_day"] <= cutoff, "新规则审计读取了截止之后的行情")
                key = r["symbol"] + "." + r["exchange"]
                if item["kind"] != "bar" or key not in keys:
                    continue
                dt = datetime.fromisoformat(r["datetime"])
                identity = key, r["trading_day"]
                if identity not in periods:
                    periods[identity] = self.calendar.periods(r["trading_day"], self.metadata.get(*identity), night=cfg["strategy"]["include_night_indicators"])
                period = next(((a, b) for a, b in periods[identity] if a <= dt < b), None)
                if period is None:
                    continue
                end = (dt+MINUTE).isoformat()
                values = {k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}
                if identity in selected:
                    self.raw[(key, end)] = values
                offset = int((dt-period[0]).total_seconds()/60)
                for n in (5, 15):
                    start = period[0] + offset//n*n*MINUTE
                    buckets[(key, n, r["trading_day"], start)].append((dt, values))
        self.frames = defaultdict(list)
        for (key, n, day, start), bucket in sorted(buckets.items()):
            bucket.sort(key=lambda r: r[0])
            if len(bucket) != n or any(dt != start+j*MINUTE for j, (dt, _) in enumerate(bucket)):
                continue
            rr = [r for _, r in bucket]
            self.frames[(key, n)].append({"end": (start+n*MINUTE).isoformat(), "day": day,
                "open": rr[0]["open"], "close": rr[-1]["close"], "high": max(r["high"] for r in rr),
                "low": min(r["low"] for r in rr), "volume": sum(r["volume"] for r in rr)})
        for records in self.frames.values():
            indicators(records, cfg["strategy"]["atr_period"])
        self.checked_bars = sum(len(r) for r in self.frames.values())
        require(file_sha256(evidence["cache"]) == evidence["cache_sha256"], "新规则的指标来源改变")
        for key, n, records in read_frames(evidence["cache"], evidence["cache_key"]):
            if key not in keys:
                continue
            require(all(r["day"] <= cutoff for r in records), "新规则指标有截止后的日期")
            if n in (5, 15):
                actual = self.frames[(key, n)]
                require(len(actual) == len(records), "独立聚合的完整高周期K线数不符")
                for a, b in zip(actual, records, strict=True):
                    for k in a:
                        equivalent(b[k], a[k], "原始高周期指标/" + k)
            elif n == 1:
                chosen = [r for r in records if (key, r["day"]) in selected]
                for r in chosen:
                    for k, v in self.raw[(key, r["end"])].items():
                        close(r[k], v, "原始分钟价格/" + k)
                self.frames[(key, 1)] = chosen
        self.times = {k: [r["end"] for r in v] for k, v in self.frames.items()}

    def past(self, key, n, time, count):
        identity = key, n
        i = bisect_right(self.times.get(identity, []), time)
        return self.frames.get(identity, [])[max(0, i-count):i]

    def period(self, row):
        dt = datetime.fromisoformat(row["time"])-MINUTE
        meta = self.metadata.get(row["contract"], row["date"])
        return next(((a, b) for a, b in self.calendar.periods(row["date"], meta) if a <= dt < b), None)

    def structure(self, row):
        period = self.period(row)
        past = self.past(row["contract"], 5, row["time"], 3)
        result = {"signal_time": row["time"], "period_open": period[0].isoformat() if period else None,
                  "ready": False, "sources": []}
        if not period or len(past) != 3:
            return result
        result["sources"] = [{k: r[k] for k in ("end", "low", "high")} for r in past]
        times = [datetime.fromisoformat(r["end"]) for r in past]
        if any(t-5*MINUTE < period[0] for t in times) or any(b-a != 5*MINUTE for a,b in zip(times[:-1], times[1:], strict=True)):
            return result
        atr = past[-1]["previous_atr"]
        if atr is None or atr <= 0:
            return result
        return result | {"ready": True, "atr_previous": atr,
                         "extreme": min(r["low"] for r in past) if row["direction"] == "LONG" else max(r["high"] for r in past),
                         "source_end": past[-1]["end"]}

    def higher(self, row, base):
        past = self.past(row["contract"], 5, row["time"], 11)
        fifteen = self.past(row["contract"], 15, row["time"], 1)
        five = past[-1] if past else None
        sign = 1 if row["direction"] == "LONG" else -1
        tick = self.metadata.get(row["contract"], row["date"])["tick_size"]
        rule = self.cfg["strategy"]["trend_quality"]
        quality = {"trend_activity": False, "trend_displacement": False}
        details = {"observations": len(past)}
        atr = five["previous_atr"] if five else None
        if len(past) == 11 and atr is not None and atr > 0:
            closes = [r["close"] for r in past]
            move = sign * (closes[-1]-closes[0])
            changes = sum(abs(b-a) > tick*1e-8 for a,b in zip(closes[:-1], closes[1:], strict=True))
            threshold = max(rule["min_displacement_atr"]*atr, rule["min_displacement_ticks"]*tick)
            quality = {"trend_activity": changes >= rule["min_price_changes"],
                       "trend_displacement": move >= threshold-tick*1e-8}
            details.update(window_start=past[0]["end"], window_end=past[-1]["end"],
                signed_move_ticks=move/tick, signed_move_atr=move/atr,
                nonzero_price_changes=changes, required_displacement=threshold,
                signed_ma20_slope5_ticks=sign*(five["ma20"]-past[-6]["ma20"])/tick if five["ma20"] is not None and past[-6]["ma20"] is not None else None,
                signed_ma40_distance_ticks=sign*(five["close"]-five["ma40"])/tick if five["ma40"] is not None else None)
        efficiency = sign*five["efficiency"] if five and five["efficiency"] is not None else None
        quality["efficiency"] = efficiency is not None and efficiency >= .45
        return {"filters": {k: base[k] for k in WANTED} | quality,
                "trend_quality": details, "efficiency": efficiency,
                "source_5m_end": five["end"] if five else None,
                "source_15m_end": fifteen[-1]["end"] if fifteen else None}


def pullback(past, sign, tick, strategy):
    if len(past) < 5 or sign*(past[-1]["close"]-(past[-2]["high"] if sign > 0 else past[-2]["low"])) <= 0:
        return None
    for i in range(len(past)-2, max(-1, len(past)-5), -1):
        row, before = past[i], past[i-1]
        if row["previous_atr"] is None or row["previous_atr"] <= 0:
            continue
        if any(before["ma"+str(n)] is None or sign*(before["close"]-before["ma"+str(n)]) <= 0 for n in (10, 20)):
            continue
        epsilon = max(strategy["pullback_epsilon_ticks"]*tick, strategy["pullback_epsilon_atr"]*row["previous_atr"])
        price = row["low"] if sign > 0 else row["high"]
        touched = [n for n in (10, 20) if row["ma"+str(n)] is not None and abs(price-row["ma"+str(n)]) <= epsilon]
        if 10 in touched:
            return {"event": row["end"], "dual_touch": len(touched)==2, "epsilon": epsilon}
    return None


def expected_pattern(row, base, evidence, windows, setups):
    higher, period = evidence.higher(row, base), evidence.period(row)
    source = datetime.fromisoformat(higher["source_5m_end"]) if higher["source_5m_end"] else None
    identity = row["date"], row["contract"], row["direction"]
    window = windows.get(identity)
    qualified = bool(all(higher["filters"].values()) and period and source and source >= period[0]+5*MINUTE)
    if not qualified:
        windows.pop(identity, None)
        setups.pop(identity, None)
        window = None
    elif window is None or window["source_5m_end"] != higher["source_5m_end"] or window["period_open"] != period[0].isoformat():
        window = {"armed_at": row["time"], "expires_at": min(source+5*MINUTE, period[1]).isoformat(),
                  "source_5m_end": higher["source_5m_end"], "period_open": period[0].isoformat()}
        windows[identity] = window
        setups.pop(identity, None)
    valid = bool(window and window["armed_at"] <= row["time"] < window["expires_at"])
    low = {k:v for k,v in base.items() if k not in REMOVED}
    eligible = valid and qualified and all(low.values())
    previous = setups.pop(identity, None)
    sign = 1 if row["direction"] == "LONG" else -1
    tick = evidence.metadata.get(row["contract"], row["date"])["tick_size"]
    clock = datetime.fromisoformat(row["time"])
    current = evidence.raw[(row["contract"], row["time"])]
    confirmed = bool(eligible and previous and previous["setup_end"] == (clock-MINUTE).isoformat() and sign*(current["close"]-previous["confirmation_level"]) > tick*1e-8)
    event = None
    if confirmed:
        event = {"event": previous["setup_end"], "kind": previous["kind"],
                 "references": [10] if previous["kind"] == "confirmed_pullback" else [],
                 "dual_touch": previous["dual_touch"], "epsilon": previous["epsilon"], "touch_event": previous["touch_event"]}
    setup = None
    past = evidence.past(row["contract"], 1, row["time"], 11)
    if eligible and len(past) >= 4 and not confirmed:
        before = past[-3:-1]
        adjacent = [r["end"] for r in past[-3:]] == [(clock-2*MINUTE).isoformat(), (clock-MINUTE).isoformat(), clock.isoformat()]
        armed = datetime.fromisoformat(window["armed_at"])
        touch = pullback(past, sign, tick, evidence.cfg["strategy"])
        touched = bool(touch and datetime.fromisoformat(touch["event"])-MINUTE >= armed)
        boundary = max(r["high"] for r in before) if sign > 0 else min(r["low"] for r in before)
        broken = adjacent and clock-MINUTE >= armed and sign*(current["close"]-boundary) > tick*1e-8
        if adjacent and (broken or touched):
            setup = {"kind": "confirmed_breakout" if broken else "confirmed_pullback",
                     "setup_end": row["time"], "setup_start": (clock-MINUTE).isoformat(),
                     "confirmation_level": current["high"] if sign > 0 else current["low"],
                     "breakout_boundary": boundary, "reference_ends": [r["end"] for r in before],
                     "window": dict(window), "higher": higher,
                     "touch_event": touch["event"] if touched else None,
                     "dual_touch": touch["dual_touch"] if touched else False,
                     "epsilon": touch["epsilon"] if touched else 0}
            setups[identity] = setup
    detail = {"qualified": qualified, "window": window, "higher": higher,
              "confirmation": previous if confirmed else None, "setup": setup,
              "confirmation_time": row["time"] if confirmed else None}
    filters = low | {"higher_trend_quality": qualified, "confirmation_window": valid, "price_pattern_confirmed": confirmed}
    return detail, filters, event, bool(setup or previous)


def protection(row, meta, price, fee_solver, context, reserved=0):
    """Reconstruct every stop floor from declared inputs, never actual stop output."""
    s = row["_strategy"]
    sign, tick, value = (1 if row["direction"] == "LONG" else -1), meta["tick_size"], meta["value_per_price"]
    atr = json.loads(row["snapshot"])["atr_previous"]
    quote = Decimal(str(price))
    costs = float(fee_solver(meta, 1, "open", quote)+fee_solver(meta, 1, "close_today", quote))
    original = s["fixed_ticks"][meta["product"]]
    floor = max(original["stop_loss_ticks"], math.ceil(atr*s["protection_scale"]["atr_multiple"]/tick-1e-9),
                math.ceil(s["protection_scale"]["roundtrip_cost_multiple"]*(costs/(tick*value)+2*s["slippage_ticks"])-1e-9), reserved)
    if not context["ready"]:
        return {"accepted": False, "rejections": ["structure_history_missing"]}
    anchor = context["extreme"]-sign*tick
    distance = sign*(price-anchor)
    ticks = math.ceil(distance/tick-1e-9)
    stop = max(floor, ticks)
    rejected = (["structure_already_broken"] if distance <= tick*1e-8 else
                ["structure_distance_exceeds_atr_cap"] if stop*tick > 2*context["atr_previous"]+tick*1e-8 else [])
    return {"accepted": not rejected, "rejections": rejected, "stop_loss_ticks": stop,
            "take_profit_ticks": math.ceil(stop*original["take_profit_ticks"]/original["stop_loss_ticks"]-1e-9),
            "structure_anchor": anchor, "structure_distance_ticks": ticks,
            "structure_atr_previous": context["atr_previous"], "structure_sources": context["sources"],
            "structure_source_end": context["source_end"], "max_stop_atr": 2.}


def audit_journal(run, cfg, fee_solver, evidence):
    plan, _ = validate_optimization(cfg)
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    ranks = {(r["date"],r["contract"]):r for r in rows(run / "daily_candidates.csv.gz")}
    rules = {(r["trading_day"],r["contract"]):r for r in cfg["execution"]["qualification"]["rules"]}
    enabled = bool(cfg["strategy"].get("entry_confirmation"))
    structural = bool(cfg["strategy"].get("structure_protection"))
    setups = iter(rows(run / "confirmation_setups.csv.gz")) if enabled else iter(())
    channels = iter(rows(run / "confirmed_channels.csv.gz")) if enabled else iter(())
    windows, pending_setups, previous_pass, consumed, counters = {}, {}, {}, set(), Counter()
    stops = defaultdict(list)
    for trade in rows(run / "trades.csv.gz"):
        if trade["exit_reason"] == "fixed_stop" and float(trade["net_pnl"]) < 0:
            stops[(trade["entry_time"][:10], trade["contract"], trade["direction"])].append(trade["exit_time"])
    resets, structural_cancellations = defaultdict(list), defaultdict(list)
    for event in rows(run / "events.csv.gz"):
        if event["action"] == "entry_cancelled" and event["reason"].startswith("fill_"):
            resets[event["contract"]].append((event["time"], event["reason"] == "fill_price_recheck"))
            if event["reason"] == "fill_structure_recheck":
                structural_cancellations[event["contract"]].append(event["time"])
    reset_indices = defaultdict(int)
    for original, row in itertools.zip_longest(rows(parent / "signals.csv.gz"), rows(run / "signals.csv.gz")):
        require(original is not None and row is not None, "固定候选分钟观察有遗漏")
        identity = row["contract"], row["time"]
        for k in ("time", "date", "contract", "product", "group", "direction", "rank", "r8", "exit_flags", "execution_pass", "execution_rejections"):
            require(row[k] == original[k], "原候选/测量或执行资格改变："+k)
        rank = ranks[(row["date"],row["contract"])]
        require(all(row[k] == rank[k] for k in ("direction", "rank", "r8", "group", "product")), "观察候选身份不符")
        snapshot, flags = json.loads(row["snapshot"]), json.loads(row["filters"])
        base = json.loads(original["filters"])
        for k in ("cost", "stop_reentry"):
            base.pop(k, None)
        base["state"] = flags["state"]
        require({k:v for k,v in snapshot.items() if k not in {"entry_channel", "price_confirmation", "structure_stop"}} == json.loads(original["snapshot"]), "未声明的基础测量改变")
        common = {"cost": flags["cost"], "stop_reentry": flags["stop_reentry"]}
        if row["execution_pass"] == "True":
            check = json.loads(row["cost_check"])
            rule = rules[(row["date"], row["contract"])]
            price = evidence.raw[identity]["close"]
            close(check["price"], price, "成本报价")
            quote = Decimal(str(price))
            fees = float(fee_solver(rule, 1, "open", quote)+fee_solver(rule, 1, "close_today", quote))
            atr = snapshot["atr_previous"]
            ratio = (fees/rule["value_per_price"]+2*cfg["strategy"]["slippage_ticks"]*rule["tick_size"])/atr if atr is not None and atr > 0 else None
            require(common["cost"] == check["accepted"] == (ratio is not None and ratio <= cfg["strategy"]["entry_cost_filter"]["max_cost_atr"]+1e-12), "公共成本门槛不符")
            if ratio is not None:
                close(check["cost_atr"], ratio, "成本ATR")
            counters["cost_checks"] += 1
        else:
            require(not common["cost"], "无执行资料仍通过成本过滤")
        allowed = not any(t <= row["time"] for t in stops[(row["date"],row["contract"],row["direction"])])
        require(common["stop_reentry"] == allowed, "止损后禁入不符")
        if not allowed:
            require(row["trigger"] == row["filled"] == "False", "绕过止损后禁入")
        if enabled:
            detail, extra, event, log_setup = expected_pattern(row, base, evidence, windows, pending_setups)
            if log_setup:
                recorded = next(setups, None)
                require(recorded is not None and (recorded["contract"], recorded["time"]) == identity, "确认事件记录有遗漏或多出")
                equivalent(json.loads(recorded["detail"]), detail, "独立确认事件")
                require((recorded["confirmed"] == "True") == bool(event), "确认结果不符")
                counters["setup_decisions"] += 1
                counters["confirmed_patterns"] += bool(event)
            state_key = row["date"], row["contract"]
            previous = previous_pass.get(state_key, False)
            events = resets[row["contract"]]
            ri = reset_indices[row["contract"]]
            while ri < len(events) and events[ri][0] <= (datetime.fromisoformat(row["time"])-MINUTE).isoformat():
                previous = events[ri][1]
                ri += 1
            reset_indices[row["contract"]] = ri
            passes = {"legacy": all(base.values()) and all(common.values()),
                      "confirmed": all(extra.values()) and all(common.values())}
            used = bool(event and (row["contract"],event["event"]) in consumed)
            triggers = {"legacy": passes["legacy"] and not previous,
                        "confirmed": passes["confirmed"] and bool(event) and not used}
            chosen = ("legacy" if triggers["legacy"] else "confirmed" if triggers["confirmed"] else
                      "legacy" if passes["legacy"] else "confirmed" if passes["confirmed"] else "legacy")
            label = "legacy" if chosen == "legacy" else event["kind"]
            expected = (base if chosen=="legacy" else extra) | common
            require(flags == expected and snapshot["entry_channel"] == label, "未匹配独立通道选择/原通道优先")
            require((row["trigger"] == "True") == triggers[chosen], "触发边沿不符")
            if chosen == "confirmed":
                equivalent(snapshot["price_confirmation"], detail, "实际确认入场快照")
                equivalent(json.loads(row["pullback"]), event, "实际价格事件")
            else:
                require(not row["pullback"] and "price_confirmation" not in snapshot, "原通道被改为确认入场")
            if any(passes.values()) or any(triggers.values()):
                record = next(channels, None)
                require(record is not None and (record["contract"],record["time"]) == identity, "通道决策记录有遗漏或多出")
                for k, v in {"chosen": chosen, "entry_channel": label, "passes": passes, "triggers": triggers,
                             "previous_legacy_pass": previous, "consumed": used, "event": event, "common": common}.items():
                    actual = json.loads(record[k]) if k in {"passes","triggers","event","common"} and record[k] else record[k] == "True" if type(v) is bool else record[k] or None
                    equivalent(actual, v, "独立通道状态/"+k)
                # The engine attaches shared gates to the original dictionary
                # after signal evaluation. Check their values on both channels.
                recorded_filters = json.loads(record["channel_filters"])
                equivalent(recorded_filters, {"legacy": base | common, "confirmed": extra}, "通道过滤明细")
                counters["channel_decisions"] += 1
            previous_pass[state_key] = passes["legacy"]
            if row["trigger"] == "True" and row["execution_pass"] == "True" and row["risk_pass"] == "False":
                previous_pass[state_key] = False
            if chosen == "confirmed" and row["trigger"] == row["risk_pass"] == "True":
                require(not used, "同一价格事件重复请求")
                consumed.add((row["contract"],event["event"]))
            counters[label+"_fills"] += row["filled"] == "True"
        else:
            require(flags == base | common, "结构方案改变了入场过滤")
        require((row["all_pass"] == "True") == all(flags.values()), "总通过标记不符")
        require(set(json.loads(row["rejections"])) == {k for k,v in flags.items() if not v}, "拒绝原因不符")
        if structural:
            context = evidence.structure(row)
            equivalent(snapshot["structure_stop"], context, "独立结构快照")
            counters["structural_snapshots"] += 1
            if row["trigger"] == row["execution_pass"] == "True":
                meta = evidence.metadata.get(row["contract"], row["date"]) | rules[(row["date"],row["contract"])]
                expected = protection(row | {"_strategy": cfg["strategy"]}, meta, evidence.raw[identity]["close"], fee_solver, context)
                if expected["accepted"]:
                    recorded_plan = row.get("protection_plan")
                    require(row["risk_pass"] != "True" or bool(recorded_plan), "已预约结构信号缺少保护计划")
                    if recorded_plan:
                        actual = json.loads(recorded_plan)
                        equivalent({k:actual[k] for k in expected}, expected, "结构预约止损")
                else:
                    require(row["risk_pass"] == row["filled"] == "False" and json.loads(row["risk_rejections"]) == expected["rejections"], "结构无效仍预约/成交")
                counters["structural_admissions"] += 1
                if row.get("fill_structure_rejections"):
                    cancelled = structural_cancellations[row["contract"]]
                    require(bool(cancelled), "结构成交取消缺少事件")
                    time = cancelled.pop(0)
                    require(time >= row["time"], "结构成交取消先于信号")
                    opening_end = (datetime.fromisoformat(time)+MINUTE).isoformat()
                    sign = 1 if row["direction"]=="LONG" else -1
                    price = evidence.raw[(row["contract"],opening_end)]["open"]+sign*cfg["strategy"]["slippage_ticks"]*meta["tick_size"]
                    expected_fill = protection(row | {"_strategy":cfg["strategy"]},meta,price,fee_solver,context,
                                               json.loads(row["protection_plan"])["stop_loss_ticks"])
                    require(not expected_fill["accepted"] and row["filled"]=="False"
                            and json.loads(row["fill_structure_rejections"])==expected_fill["rejections"], "独立成交结构复核不符")
                    counters["structural_fill_cancellations"] += 1
        counters["observations"] += 1
    require(next(setups, None) is None and next(channels, None) is None, "多余确认/通道决策记录")
    require(not any(structural_cancellations.values()), "结构成交取消事件未关联到预约信号")
    if enabled:
        signal_orders = {(r["contract"],r["time"]):json.loads(r["snapshot"])["entry_channel"] for r in rows(run/"signals.csv.gz") if r["risk_pass"]=="True"}
        admitted = defaultdict(list)
        for order in rows(run / "orders.csv.gz"):
            rank = ranks[(order["time"][:10],order["contract"])]
            priority = ({"legacy":0,"confirmed_breakout":1,"confirmed_pullback":2}[signal_orders[(order["contract"],order["time"])]],
                        int(rank["rank"]), -abs(float(rank["r8"])), rank["group"], order["contract"])
            admitted[order["time"]].append(priority)
        require(all(v == sorted(v) for v in admitted.values()), "同分钟未按通道和原候选次序预占资金")
    return {"status": "passed", **dict(counters), "all_pattern_decisions_reconstructed": enabled,
            "raw_completed_higher_bars_and_wilder_atr_checked": evidence.checked_bars,
            "source_data_sha256": evidence.source_sha, "future_measurements_used": False}


def audit(directory):
    run = Path(directory)
    cfg = json.loads((run / "config_snapshot.json").read_text())
    plan, baseline = validate_optimization(cfg)
    variant = cfg["optimization_review"]["variant"]
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    if variant != "control":
        freeze_path = Path(cfg["optimization_review"]["plan"]).with_name("implementation_freeze.json")
        freeze = json.loads(freeze_path.read_text())
        manifest = json.loads((run/"manifest.json").read_text())
        require(freeze["plan_sha256"]==file_sha256(cfg["optimization_review"]["plan"]), "实现冻结未匹配本轮声明")
        for name, checksum in freeze["strategy_and_runner_hashes"].items():
            if name.startswith("research/"):
                require(manifest["source_hashes"][name]==checksum, "回放实现改变了冻结规则："+name)
    if variant == "control":
        from .ordered_opportunity_audit import audit as original_control_audit
        result = original_control_audit(run)
        result["structure_round"] = {"status": "passed", "enabled": False, "exact_original_control": True}
    else:
        evidence = Evidence(run, cfg)
        base_helper = coverage_audit.helper
        structural_checks = []

        def helper(name):
            module, source = base_helper(name)
            if name == "audit_slope_band" and cfg["strategy"].get("entry_confirmation"):
                original = module.audit_slope
                def slope(signal, frames, configuration, metadata):
                    actual = copy.deepcopy(configuration)
                    if json.loads(signal["snapshot"])["entry_channel"] != "legacy":
                        actual["strategy"]["slope_band"]["timeframes"] = {"5m": configuration["strategy"]["slope_band"]["timeframes"]["5m"]}
                    return original(signal, frames, actual, metadata)
                module.audit_slope = slope
            if name == "audit_trailing_exit" and cfg["strategy"].get("structure_protection"):
                original = module.audit_trades
                def trades(directory, configuration, signals):
                    adapted = copy.deepcopy(signals)
                    rules = {(r["trading_day"],r["contract"]):r for r in configuration["execution"]["qualification"]["rules"]}
                    for trade in rows(directory / "trades.csv.gz"):
                        identity = trade["contract"],trade["entry_signal_time"]
                        signal = signals[identity]
                        context = evidence.structure(signal)
                        plan_stop = json.loads(signal["protection_plan"])["stop_loss_ticks"]
                        meta = evidence.metadata.get(trade["contract"],trade["entry_time"][:10]) | rules[(trade["entry_time"][:10],trade["contract"])]
                        expected = protection(signal | {"_strategy":configuration["strategy"]}, meta, float(trade["entry_price"]), module.fee, context, plan_stop)
                        require(expected["accepted"], "无效结构仍成交")
                        actual = json.loads(trade["entry_protection"])
                        equivalent({k:actual[k] for k in expected}, expected, "结构成交止损")
                        plan_copy = json.loads(adapted[identity]["protection_plan"])
                        plan_copy["stop_loss_ticks"] = expected["stop_loss_ticks"]
                        adapted[identity]["protection_plan"] = json.dumps(plan_copy)
                        structural_checks.append({"contract":trade["contract"],"entry_time":trade["entry_time"],
                                                  "expected_stop_ticks":expected["stop_loss_ticks"],"anchor":expected["structure_anchor"],"passed":True})
                    # Only the independently computed fill-distance floor is
                    # adapted. Fees, risk, breakeven, trail and every raw exit
                    # minute still go through the unchanged arithmetic checker.
                    return original(directory, configuration, adapted)
                module.audit_trades = trades
            return module, source

        def journal(directory, configuration, fee_solver):
            return audit_journal(directory, configuration, fee_solver, evidence)

        def pullbacks(configuration, trades, signals, frames, indices):
            # The entire two-stage confirmation, including MA10 touch geometry,
            # is reconstructed by the journal audit above.
            return {"status":"passed","verified_in":"full_confirmation_state_audit"}

        with patch.object(coverage_audit,"helper",helper), patch.object(coverage_audit,"audit_journal",journal), patch.object(coverage_audit,"audit_pullbacks",pullbacks):
            result = coverage_audit.audit_run(run)
        result["structure_round"] = {"status":"passed","independent_structural_fills":structural_checks,
            "fill_floor_adapter_uses_only_independently_recomputed_stop":True,
            "frozen_geometry_and_atr_rebuilt_from_raw_minutes":True,
            "all_confirmations_and_channel_choices_checked":bool(cfg["strategy"].get("entry_confirmation"))}
        exact = {n:file_sha256(run/(n+".csv.gz"))==file_sha256(parent/(n+".csv.gz")) for n in ("daily_pool","pool_exclusions","daily_candidates","candidate_execution")}
        require(all(exact.values()), "原池/排名改变")
        result["exact_csv_matches"] = exact
    require(cfg["risk"] == baseline["risk"] and cfg["strategy"]["breakeven"] == {"activation_r":1.,"include_costs":True}
            and cfg["strategy"]["trailing_exit"] == baseline["strategy"]["trailing_exit"], "原风险或保本/追踪改变")
    result["plan_sha256"] = file_sha256(cfg["optimization_review"]["plan"])
    result["auditor_sha256"] = file_sha256(__file__)
    write_json(run / plan["audit_filename"],result,SpaceBudget(plan["budget"]))
    deduplicate(run,parent,plan)
    print(json.dumps({"phase":"structure_audit","status":"passed","variant":variant,
                      "directory":str(run),"trades":len(result["trades"]),"net":result["net"]},ensure_ascii=False),flush=True)
    return result


def deduplicate(run, parent, plan):
    links = []
    for path in run.glob("*.csv.gz"):
        source = parent/path.name
        if not source.exists():
            continue
        before, original = path.stat(), source.stat()
        if (before.st_dev,before.st_ino)==(original.st_dev,original.st_ino):
            continue
        if (before.st_mode,before.st_uid,before.st_gid,before.st_size)!=(original.st_mode,original.st_uid,original.st_gid,original.st_size):
            continue
        checksum = file_sha256(source)
        if file_sha256(path) != checksum:
            continue
        temporary = path.with_suffix(path.suffix+".link.partial")
        os.link(source,temporary)
        os.replace(temporary,path)
        require(file_sha256(path)==checksum,"独立审计后的去重指纹不符")
        links.append({"path":str(path),"source":str(source),"sha256":checksum,"released_bytes":before.st_size})
    write_json(run/"structure_immutable_deduplication.json",{"verified_links":links,"released_bytes":sum(r["released_bytes"] for r in links)},SpaceBudget(plan["budget"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",required=True)
    audit(parser.parse_args().directory)
