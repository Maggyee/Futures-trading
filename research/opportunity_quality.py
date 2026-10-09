"""Frozen opportunity labels; never produce orders or strategy backtests."""

import argparse
import bisect
import csv
import gzip
import json
import math
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar import MINUTE, stamp
from .config import ResearchError, read_config
from .coverage_expansion import ORIGINAL, ROOT
from .data import file_sha256
from .execution import RiskAllocator, can_fill, fee, slipped
from .execution_parameters import ExecutionParameters
from .experiments import code_identity
from .prepared_review import prepare_review
from .refinements import admissible_entry_price, entry_price_guard, scaled_protection
from .reporting import write_json
from .signals import Features, SignalLogic, finite, rank_candidates
from .storage import restore_dataset, write_gzip_json

PLAN = ROOT / "research/opportunity_quality_plan.json"
FREEZE = ROOT / "research/opportunity_quality_freeze.json"
FILTERS = ("oi", "efficiency", "slope_1m", "slope_5m", "ma10_recovery",
           "ma20_recovery", "original_market_gate")


def require(condition, message):
    if not condition:
        raise ResearchError(message)


def csv_rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def read_records(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def rank_group(rank):
    return "rank_1_2" if rank <= 2 else "rank_3_5" if rank <= 5 else "rank_6_plus"


def merge_segments(rows, gap=30):
    """Keep the first causal clock, even if a condition passes only later."""
    previous = {}
    for row in sorted(rows, key=lambda r: (r["date"], r["contract"], r["time"])):
        key = row["date"], row["contract"], row["direction"], row["period"]
        old = previous.get(key)
        first = old is None or row["minute_index"] - old["minute_index"] > gap
        row["representative"] = first
        row["segment_id"] = row["id"] if first else old["segment_id"]
        previous[key] = row
    return rows


def match_observations(rows, rule):
    """Match using only contemporaneous fields; never inspect label availability."""
    clocks = defaultdict(list)
    for row in rows:
        clocks[(row["date"], row["time"], row["group"], row["session_profile"], row["direction"])].append(row)
    pairs = []
    for key, cohort in sorted(clocks.items()):
        for treatment, control in rule["comparisons"]:
            used = set()
            targets = sorted((r for r in cohort if r["representative"] and r["rank_group"] == treatment),
                             key=lambda r: (r["rank"], r["contract"]))
            for target in targets:
                options = []
                for other in cohort:
                    if other["rank_group"] != control or other["id"] in used:
                        continue
                    if min(target["previous_volume"], other["previous_volume"],
                           target["relative_atr"], other["relative_atr"]) <= 0:
                        continue
                    liquidity = abs(math.log(target["previous_volume"] / other["previous_volume"]))
                    volatility = abs(math.log(target["relative_atr"] / other["relative_atr"]))
                    if liquidity > math.log(rule["maximum_liquidity_ratio"]) or volatility > math.log(rule["maximum_relative_atr_ratio"]):
                        continue
                    options.append((liquidity + volatility, other["contract"], other, liquidity, volatility))
                if options:
                    distance, _, other, liquidity, volatility = min(options, key=lambda x: (x[0], x[1]))
                    used.add(other["id"])
                    pairs.append({"comparison": treatment + "_vs_" + control, "date": key[0],
                                  "time": key[1], "treatment": target["id"], "control": other["id"],
                                  "distance": distance, "liquidity_log_difference": liquidity,
                                  "relative_atr_log_difference": volatility})
    return pairs


class PullbackLabels:
    """One pending touch; MA20 recovery and MA10 reclaim share its lifecycle."""

    def __init__(self, direction, tick, epsilon_ticks, epsilon_atr, wait=10):
        self.sign = 1 if direction == "LONG" else -1
        self.tick, self.epsilon_ticks, self.epsilon_atr, self.wait = tick, epsilon_ticks, epsilon_atr, wait
        self.pending = None
        self.events = []

    def cancel(self, clock, reason):
        self.events.append({"time": clock.isoformat(), "touch_id": self.pending["touch_id"],
                            "reference": self.pending["reference"], "action": "cancelled", "reason": reason,
                            "already_recovered": self.pending["recovered"],
                            "awaiting_ma10_reclaim": self.pending["reference"] == "20" and self.pending["recovered"]})
        self.pending = None

    def step(self, bar, one, previous, higher, continuous, period, before_cutoff):
        sign, clock, emitted = self.sign, bar.end, []
        pending = self.pending
        if pending:
            reason = None
            if period != pending["period"]:
                reason = "session_changed"
            elif not continuous or previous is None or previous["end"] != bar.datetime:
                reason = "missing_adjacent_minute"
            elif not before_cutoff:
                reason = "entry_cutoff"
            elif clock >= pending["expires"]:
                reason = "expired"
            elif not higher:
                reason = "higher_invalid"
            elif sign * ((bar.low if sign > 0 else bar.high) - pending["extreme"]) < -self.tick - self.tick * 1e-8:
                reason = "touch_extreme_broken"
            if reason:
                self.cancel(clock, reason)
                pending = None
            else:
                recovery = sign * (bar.close - (previous["high"] if sign > 0 else previous["low"])) > self.tick * 1e-8
                reference = 20 if pending["reference"] == "dual" else int(pending["reference"])
                recovery = recovery and sign * (bar.close - one[f"ma{reference}"]) > 0
                reclaim = sign * (bar.close - one["ma10"]) > 0
                if recovery and not pending["recovered"]:
                    event = {"time": clock.isoformat(), "touch_id": pending["touch_id"],
                             "reference": pending["reference"], "action": "recovered", "reclaimed_ma10": reclaim}
                    self.events.append(event)
                    emitted.append(event)
                    pending["recovered"] = True
                if recovery and reclaim and pending["reference"] == "20" and not pending["reclaimed"]:
                    event = {"time": clock.isoformat(), "touch_id": pending["touch_id"],
                             "reference": "20", "action": "reclaimed_ma10", "reclaimed_ma10": True}
                    self.events.append(event)
                    emitted.append(event)
                    pending["reclaimed"] = True
                if pending["recovered"] and (pending["reference"] != "20" or pending["reclaimed"]):
                    self.pending = None
        if self.pending is None and not emitted and higher and continuous and before_cutoff and previous is not None:
            if previous["end"] != bar.datetime or not all(finite(previous.get(f"ma{m}")) for m in (10, 20)):
                return emitted
            if not (sign * (previous["ma10"] - previous["ma20"]) > 0
                    and all(sign * (previous["close"] - previous[f"ma{m}"]) > 0 for m in (10, 20))):
                return emitted
            epsilon = max(self.epsilon_ticks * self.tick, self.epsilon_atr * one["previous_atr"])
            extreme = bar.low if sign > 0 else bar.high
            touched = [m for m in (10, 20) if abs(extreme - one[f"ma{m}"]) <= epsilon]
            if touched:
                reference = "dual" if len(touched) == 2 else str(touched[0])
                self.pending = {"touch_id": clock.isoformat(), "reference": reference, "extreme": extreme,
                                "period": period, "expires": clock + self.wait * MINUTE,
                                "recovered": False, "reclaimed": False}
                self.events.append({"time": clock.isoformat(), "touch_id": clock.isoformat(),
                                    "reference": reference, "action": "formed", "epsilon": epsilon,
                                    "extreme": extreme, "expires": self.pending["expires"].isoformat()})
        return emitted


class LabelStudy:
    def __init__(self, data, features, plan):
        self.data, self.features, self.plan = data, features, plan
        self.logic = SignalLogic(data, features)
        self.parameters = ExecutionParameters(data.cfg, data.metadata)
        self.allocator = RiskAllocator(data.cfg)
        self.schedule = {}

    def minutes(self, day, key):
        identity = day, key
        if identity not in self.schedule:
            self.schedule[identity] = self.data.calendar.minutes(day, self.data.metadata.get(key, day))
        return self.schedule[identity]

    def observe(self, bar, candidate, period, recent=None, source="grid"):
        result = self.logic.evaluate(bar, candidate, before_cutoff=True)
        flags, snapshot = result["filters"], result["snapshot"]
        atr = snapshot["atr_previous"]
        if not finite(atr) or atr <= 0 or not finite(snapshot.get("ma20")):
            return None
        selected = {"oi": flags["oi"], "efficiency": flags["efficiency"],
                    "original_market_gate": all(v for k, v in flags.items() if k not in {"candidate", "state", "entry_time"})}
        for minutes in (1, 5):
            selected[f"slope_{minutes}m"] = all(flags.get(f"slope_{minutes}m_{k}", False) for k in ("ready", "minimum", "maximum"))
        for reference in (10, 20):
            event = (recent or {}).get(str(reference))
            selected[f"ma{reference}_recovery"] = bool(event and stamp(event["time"]) > bar.end - 5 * MINUTE)
        meta, reasons = self.parameters.resolve(bar.key, bar.end)
        record = {"id": source + "/" + bar.trading_day + "/" + bar.key + "/" + bar.end.isoformat(),
                  "source": source, "time": bar.end.isoformat(), "date": bar.trading_day,
                  "contract": bar.key, "product": candidate["product"], "group": candidate["group"],
                  "direction": candidate["direction"], "rank": candidate["rank"], "rank_group": rank_group(candidate["rank"]),
                  "session_profile": self.data.metadata.get(bar.key, bar.trading_day)["session_profile"],
                  "period": period, "minute_index": bisect.bisect_left(self.minutes(bar.trading_day, bar.key), bar.datetime),
                  "previous_volume": candidate["previous_volume"], "relative_atr": atr / bar.close,
                  "signal_price": bar.close, "atr": atr, "ma20": snapshot["ma20"], "r8": candidate["r8"],
                  "conditions": selected, "snapshot": snapshot, "market_rejections": result["rejections"],
                  "execution_rejections": reasons, "cost_pass": None, "quantity_signal_empty": None,
                  "minimum_capital": None, "labels": {}}
        if meta is not None:
            s, risk = self.data.cfg["strategy"], self.data.cfg["risk"]
            costs = fee(meta, bar.trading_day, "open", bar.close, 1) + fee(meta, bar.trading_day, "close_today", bar.close, 1)
            distance = costs / meta["value_per_price"] + 2 * s["slippage_ticks"] * meta["tick_size"]
            signal = {"snapshot": snapshot, "time": record["time"], "direction": candidate["direction"]}
            protection = scaled_protection(signal, meta, s, costs)
            quantity, _, _, rejects = self.allocator.allocate(meta, bar.close, bar.trading_day, {}, risk["initial_capital"],
                remaining_open_lots=meta.get("daily_open_limit"), stop_loss_ticks=protection["stop_loss_ticks"])
            per_risk = protection["stop_loss_ticks"] * meta["tick_size"] * meta["value_per_price"] + risk["cost_buffer_multiple"] * distance * meta["value_per_price"]
            per_margin = bar.close * meta["value_per_price"] * meta["margin_rate"]
            fraction, minimum = risk["group_fractions"].get(meta["group"], 0), meta.get("min_open_lots", 1)
            fractions = {"single_trade_risk": risk["trade_risk_fraction"], "portfolio_risk": risk["portfolio_risk_fraction"],
                         "group_risk": risk["portfolio_risk_fraction"] * fraction, "margin": risk["margin_fraction"],
                         "group_margin": risk["margin_fraction"] * fraction}
            needs = {k: minimum * (per_margin if "margin" in k else per_risk) / v if v > 0 else None for k, v in fractions.items()}
            record.update(cost_distance=distance, cost_atr=distance / atr, cost_pass=distance / atr <= s["entry_cost_filter"]["max_cost_atr"] + 1e-12,
                          signal_tick_size=meta["tick_size"], signal_value_per_price=meta["value_per_price"],
                          protection=protection, quantity_signal_empty=quantity, account_signal_rejections=rejects,
                          minimum_capital=max(needs.values()) if all(v is not None for v in needs.values()) else None,
                          required_by_limit=needs, execution_reference=meta.get("execution_reference"))
        return record

    def labels(self, row):
        """Future prices are confined to this function, after cohort/match selection."""
        day, key, clock = row["date"], row["contract"], stamp(row["time"])
        sign = 1 if row["direction"] == "LONG" else -1
        schedule = self.minutes(day, key)
        start = bisect.bisect_left(schedule, clock)
        bars = self.data.by_day.get((day, key), {})
        labels = {}
        for horizon in self.plan["forward_labels"]["horizons"]:
            result = {"horizon": horizon, "raw_status": "censored", "economic_status": "unknown"}
            labels[str(horizon)] = result
            times = schedule[start:start + horizon]
            if len(times) != horizon:
                result["reason"] = "day_ends_before_horizon"
                continue
            path = [bars.get(t) for t in times]
            if any(b is None for b in path):
                result["reason"] = "scheduled_minute_missing"
                continue
            first, last, atr = path[0], path[-1], row["atr"]
            entry, exit_price = first.open, last.close
            move = sign * (exit_price - entry)
            favorable = max([0.] + [sign * ((b.high if sign > 0 else b.low) - entry) for b in path])
            adverse = max([0.] + [sign * (entry - (b.low if sign > 0 else b.high)) for b in path])
            result.update(raw_status="complete", entry_time=first.datetime.isoformat(), exit_time=last.end.isoformat(),
                          raw_entry=entry, raw_exit=exit_price, raw_atr=move / atr, raw_bps=move / entry * 10000,
                          mfe_atr=favorable / atr, mae_atr=adverse / atr,
                          mfe_less_signal_cost_atr=(favorable - row["cost_distance"]) / atr if "cost_distance" in row else None)
            meta_entry, entry_rejects = self.parameters.resolve(key, first.datetime)
            meta_exit, exit_rejects = self.parameters.resolve(key, last.end - MINUTE / 1000000)
            fill_entry, entry_reason = can_fill(first)
            fill_exit, exit_reason = can_fill(last)
            problems = list(row["execution_rejections"]) + entry_rejects + exit_rejects
            problems += [x for x in (entry_reason, exit_reason) if x]
            if problems or not fill_entry or not fill_exit:
                result["reason"] = "execution_unavailable"
                result["execution_rejections"] = list(dict.fromkeys(problems))
                continue
            units = row["signal_tick_size"], row["signal_value_per_price"]
            if units != (meta_entry["tick_size"], meta_entry["value_per_price"]) or units != (meta_exit["tick_size"], meta_exit["value_per_price"]):
                result["reason"] = "contract_units_changed"
                continue
            s, risk = self.data.cfg["strategy"], self.data.cfg["risk"]
            modeled_entry = slipped(entry, sign, meta_entry, s["slippage_ticks"])
            modeled_exit = slipped(exit_price, -sign, meta_exit, s["slippage_ticks"])
            fees = fee(meta_entry, day, "open", modeled_entry, 1) + fee(meta_exit, day, "close_today", modeled_exit, 1)
            net = sign * (modeled_exit - modeled_entry) - fees / meta_entry["value_per_price"]
            signal = {"snapshot": row["snapshot"], "time": row["time"], "direction": row["direction"]}
            guard = entry_price_guard(signal, meta_entry, s)
            guard_pass = admissible_entry_price(guard, modeled_entry)
            entry_fees = fee(meta_entry, day, "open", modeled_entry, 1) + fee(meta_entry, day, "close_today", modeled_entry, 1)
            cost_at_open = (entry_fees / meta_entry["value_per_price"] + 2 * s["slippage_ticks"] * meta_entry["tick_size"]) / atr
            cost_pass_at_open = cost_at_open <= s["entry_cost_filter"]["max_cost_atr"] + 1e-12
            protection = scaled_protection(signal, meta_entry, s, entry_fees, row["protection"]["stop_loss_ticks"])
            quantity, _, _, rejects = self.allocator.allocate(meta_entry, modeled_entry, day, {}, risk["initial_capital"],
                maximum=row["quantity_signal_empty"], remaining_open_lots=meta_entry.get("daily_open_limit"),
                stop_loss_ticks=protection["stop_loss_ticks"])
            extra_entry = slipped(entry, sign, meta_entry, s["slippage_ticks"] + 1)
            extra_exit = slipped(exit_price, -sign, meta_exit, s["slippage_ticks"] + 1)
            extra_fees = fee(meta_entry, day, "open", extra_entry, 1) + fee(meta_exit, day, "close_today", extra_exit, 1)
            extra_net = sign * (extra_exit - extra_entry) - extra_fees / meta_entry["value_per_price"]
            result.update(economic_status="complete", modeled_entry=modeled_entry, modeled_exit=modeled_exit,
                          roundtrip_fee_cny=fees, net_per_lot_cny=net * meta_entry["value_per_price"],
                          net_atr=net / atr, net_bps=net / entry * 10000, extra_tick_net_atr=extra_net / atr,
                          price_guard_pass=guard_pass, quantity_empty=quantity, account_rejections=rejects,
                          cost_at_open=cost_at_open, cost_pass_at_open=cost_pass_at_open,
                          quantity_and_guard_feasible=bool(quantity > 0 and guard_pass and row["cost_pass"] and cost_pass_at_open),
                          isolated_quantity_net_cny=quantity * net * meta_entry["value_per_price"] if guard_pass and row["cost_pass"] and cost_pass_at_open else None,
                          initial_risk_distance=protection["stop_loss_ticks"] * meta_entry["tick_size"])
        return labels

    def scan_candidate(self, day, candidate):
        key, sign = candidate["contract"], 1 if candidate["direction"] == "LONG" else -1
        meta = self.data.metadata.get(key, day)
        schedule = self.minutes(day, key)
        rows = self.data.by_day.get((day, key), {})
        present = [t for t in schedule if t in rows]
        clocks = pd.DatetimeIndex([t + MINUTE for t in present])
        if not len(clocks):
            return [], [], [], {"scheduled": len(schedule), "missing": len(schedule)}
        frames = {}
        for minutes in (1, 5, 15):
            source = self.features.frames.get((key, minutes))
            if source is None or source.empty:
                return [], [], [], {"scheduled": len(schedule), "missing": len(schedule) - len(present), "indicator_missing": 1}
            frames[minutes] = source.reindex(clocks, method="ffill" if minutes != 1 else None)
        one, five, fifteen = frames[1], frames[5], frames[15]
        opening = schedule[0]
        cutoff, _, _ = self.data.calendar.deadlines(day, meta, self.data.cfg["strategy"]["times"])
        ready = np.isfinite(one[["ma10", "ma20", "ma40", "previous_atr"]]).all(axis=1) & (one.previous_atr > 0)
        higher = (np.isfinite(five.ma20) & np.isfinite(fifteen[["ma10", "ma20", "ma20_slope3"]]).all(axis=1)
                  & (fifteen.day == day) & (clocks >= opening + 15 * MINUTE)
                  & (sign * (fifteen.ma10 - fifteen.ma20) > 0) & (sign * fifteen.ma20_slope3 > 0)
                  & (sign * (fifteen.close - fifteen.ma20) > 0) & (sign * (five.close - five.ma20) > 0))
        observations, shapes = [], []
        s = self.data.cfg["strategy"]
        tracker = PullbackLabels(candidate["direction"], meta["tick_size"], s["pullback_epsilon_ticks"],
                                 s["pullback_epsilon_atr"], self.plan["pullback"]["wait_trading_minutes"])
        records = one.to_dict("records")
        continuous, recent, previous = True, {}, None
        indices = {t: i for i, t in enumerate(schedule)}
        for i, t in enumerate(present):
            bar, record = rows[t], records[i]
            position = indices[t]
            continuous = continuous and position == i
            period = self.data.calendar.locate(t, day, meta)[0].isoformat()
            clock = t + MINUTE
            if not ready.iloc[i]:
                if tracker.pending:
                    tracker.cancel(clock, "indicator_unavailable")
                previous = None
                continue
            events = tracker.step(bar, record, previous, bool(higher.iloc[i]), continuous, period, clock < cutoff)
            for event in events:
                if event["action"] == "recovered":
                    recent[event["reference"]] = event
                shape = self.observe(bar, candidate, period, recent, "shape_" + event["action"])
                if shape:
                    shape.update(reference=event["reference"], touch_id=event["touch_id"],
                                 shape_action=event["action"], reclaimed_ma10=event["reclaimed_ma10"])
                    shapes.append(shape)
            elapsed = int((clock - opening).total_seconds() / 60)
            if (elapsed % self.plan["observation"]["grid_minutes"] == 0 and clock < cutoff
                    and continuous and higher.iloc[i] and previous is not None and previous["end"] == t
                    and sign * (bar.close - record["ma20"]) > 0 and sign * (bar.close - previous["close"]) > 0):
                observation = self.observe(bar, candidate, period, recent)
                if observation:
                    observations.append(observation)
            previous = record | {"end": clock}
        if tracker.pending:
            tracker.cancel(present[-1] + MINUTE, "end_of_observation")
        lifecycle = [event | {"date": day, "contract": key, "direction": candidate["direction"],
                              "rank": candidate["rank"], "rank_group": rank_group(candidate["rank"])} for event in tracker.events]
        return observations, shapes, lifecycle, {"scheduled": len(schedule), "missing": len(schedule) - len(present)}

    def legacy(self, month, candidates):
        layer_root = ROOT / "research_outputs/rule_layers_2026-10-08"
        cases, sources = [], {}
        for variant, filename in (("control", "opportunity_diagnostics.csv.gz"), ("lifetime", "confirmation_events.csv.gz")):
            pointer = json.loads((layer_root / f"{month}_{variant}_latest.json").read_text())
            path = Path(pointer["directory"]) / filename
            sources[str(path.relative_to(ROOT))] = file_sha256(path)
            for original in csv_rows(path):
                if variant == "lifetime" and original["action"] != "formed":
                    continue
                candidate = candidates.get((original["date"], original["contract"]))
                clock = stamp(original["time"])
                bar = self.data.by_day.get((original["date"], original["contract"]), {}).get(clock - MINUTE)
                require(candidate is not None and bar is not None, "原形态的合约/时刻无法映射")
                require(candidate["direction"] == original["direction"], "原形态方向改变")
                period = self.data.calendar.locate(bar.datetime, bar.trading_day, self.data.metadata.get(bar.key, bar.trading_day))[0].isoformat()
                row = self.observe(bar, candidate, period, source="legacy_" + variant)
                require(row is not None, "原形态的因果指标缺失")
                row["published"] = original
                cutoff, _, _ = self.data.calendar.deadlines(bar.trading_day, self.data.metadata.get(bar.key, bar.trading_day), self.data.cfg["strategy"]["times"])
                row["before_cutoff"] = clock < cutoff
                row["labels"] = self.labels(row)
                cases.append(row)
        merge_segments(cases)
        return cases, sources


def freeze():
    require(not FREEZE.exists(), "研究实现已冻结，禁止覆盖")
    require(not (ROOT / json.loads(PLAN.read_text())["output"]).exists(), "已有标签，不能事后冻结")
    tracked = subprocess.check_output(["git", "ls-files", "research_outputs"], cwd=ROOT, text=True).splitlines()
    source = code_identity()
    proof = {"schema": 1, "plan_sha256": file_sha256(PLAN), "source_hashes": source["source_hashes"],
             "baseline_commit": source["git_commit"], "prior_result_hashes": {p: file_sha256(ROOT / p) for p in tracked}}
    write_json(FREEZE, proof)
    print(json.dumps({"phase": "frozen", "source_files": len(proof["source_hashes"]), "prior_result_files": len(tracked),
                      "plan_sha256": proof["plan_sha256"]}), flush=True)


def verify_freeze():
    proof = json.loads(FREEZE.read_text())
    require(proof["plan_sha256"] == file_sha256(PLAN), "研究声明已改变")
    require(proof["source_hashes"] == code_identity()["source_hashes"], "研究源码已改变，另立版本，不覆盖标签")
    return proof


def run_month(month):
    verify_freeze()
    plan = json.loads(PLAN.read_text())
    ref = plan["months"][month]
    parent = ROOT / ref["directory"]
    for filename, key in (("config_snapshot.json", "config_sha256"), ("manifest.json", "manifest_sha256"), ("trades.csv.gz", "trades_sha256")):
        require(file_sha256(parent / filename) == ref[key], "原对照来源改变：" + filename)
    output = ROOT / plan["output"] / month
    require(not output.exists(), "窗口扫描已有输出，禁止重写或增加尝试")
    output.mkdir(parents=True)
    write_json(output / "attempt.json", {"status": "started", "month": month, "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE)})
    cfg = read_config(parent / "config_snapshot.json")
    window = ref["window"]
    require(window["end"] < cfg["splits"]["test"]["start"], "拒绝读取锁定测试")
    print(json.dumps({"phase": "loading", "month": month}), flush=True)
    if month == "2026-09":
        data, features, evidence = prepare_review(ORIGINAL, cfg)
    else:
        data = restore_dataset(parent, cfg)
        require(data is not None and all(b.trading_day <= window["end"] for b in data.bars), "数据超出开发窗口")
        features = Features(data, cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (features.cache_key + ".jsonl.gz")
        evidence = {"source_run": str(parent), "data_fingerprint": data.fingerprint,
                    "cache_sha256": file_sha256(cache), "cache_key": features.cache_key, "locked_test_read": False}
    study = LabelStudy(data, features, plan)
    study.parameters.preflight({m["product"] for m in cfg["metadata"]["contracts"]}, window["start"], window["end"], data)
    observations, shapes, lifecycle, coverage, candidates = [], [], [], [], {}
    for day in ref["trading_days"]:
        pool, pool_rejections = data.pool(day)
        cutoff = max(data.calendar.bounds(day, r["meta"])[0] + 8 * MINUTE for r in pool)
        ranks, ranking_rejections = rank_candidates(data, day, pool, cutoff, 2)
        coverage.append({"date": day, "pool_count": len(pool), "rank_count": len(ranks),
                         "pool_rejections": pool_rejections, "ranking_rejections": ranking_rejections})
        for candidate in ranks:
            candidates[(day, candidate["contract"])] = candidate
            obs, shape, events, count = study.scan_candidate(day, candidate)
            observations.extend(obs)
            shapes.extend(shape)
            lifecycle.extend(events)
            coverage.append({"date": day, "contract": candidate["contract"], "rank": candidate["rank"],
                             "rank_group": rank_group(candidate["rank"]), "base_observations": len(obs), **count})
        print(json.dumps({"phase": "observed", "month": month, "day": day, "observations": len(observations), "shape_versions": len(shapes)}), flush=True)
    merge_segments(observations)
    pairs = match_observations(observations, plan["matching"])
    needed = {p[k] for p in pairs for k in ("treatment", "control")}
    for row in observations:
        if row["representative"] or row["id"] in needed:
            row["labels"] = study.labels(row)
    for row in shapes:
        row["labels"] = study.labels(row)
    legacy, legacy_sources = study.legacy(month, candidates)
    evidence["legacy_source_hashes"] = legacy_sources
    files = {"observations": observations, "shapes": shapes, "shape_lifecycle": lifecycle, "matches": pairs, "legacy_cases": legacy}
    for name, records in files.items():
        write_gzip_json(output / (name + ".json.gz"), records)
    write_json(output / "coverage.json", coverage)
    write_json(output / "sources.json", evidence)
    write_json(output / "config_snapshot.json", cfg)
    write_json(output / "attempt.json", {"status": "completed", "month": month,
        "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE)})
    manifest = {"status": "completed", "month": month, "window": window, "days": len(ref["trading_days"]),
                "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE),
                "counts": {k: len(v) for k, v in files.items()},
                "segments": sum(r["representative"] for r in observations),
                "files": {str(p.name): file_sha256(p) for p in sorted(output.iterdir()) if p.is_file()},
                "full_strategy_backtests": 0, "locked_test_read": False}
    write_json(output / "manifest.json", manifest)
    print(json.dumps(manifest | {"files": "see manifest"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "assess"))
    parser.add_argument("--month", choices=("2026-07", "2026-08", "2026-09"))
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
    elif args.action == "run":
        if not args.month:
            parser.error("run requires --month")
        run_month(args.month)
    else:
        from .opportunity_quality_assessment import assess
        assess()


if __name__ == "__main__":
    main()
