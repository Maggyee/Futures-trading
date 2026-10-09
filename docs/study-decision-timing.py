"""A finite, frozen study of base, first-strict and next-open decision clocks."""

import argparse
import bisect
import copy
import gc
import json
import math
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.calendar import MINUTE, stamp  # noqa: E402
from research.config import digest, read_config  # noqa: E402
from research.coverage_expansion import ORIGINAL  # noqa: E402
from research.data import file_sha256  # noqa: E402
from research.execution import can_fill, fee, slipped  # noqa: E402
from research.execution_parameters import ExecutionParameters  # noqa: E402
from research.opportunity_quality import (  # noqa: E402
    LabelStudy,
    csv_rows,
    rank_group,
    read_records,
    require,
)
from research.opportunity_quality_assessment import (  # noqa: E402
    daily_values,
    estimate,
    pair_summary,
)
from research.prepared_review import prepare_review  # noqa: E402
from research.refinements import (  # noqa: E402
    admissible_entry_price,
    entry_price_guard,
    scaled_protection,
)
from research.reporting import write_json  # noqa: E402
from research.signals import Features, rank_candidates  # noqa: E402
from research.storage import restore_dataset, write_gzip_json  # noqa: E402

PLAN = ROOT / "docs/decision-timing-plan.json"
FREEZE = ROOT / "docs/decision-timing-freeze.json"


def read_plan():
    return json.loads(PLAN.read_text())


def preserve(with_inputs=False):
    frozen = json.loads(FREEZE.read_text())
    require(file_sha256(PLAN) == frozen["plan_sha256"], "决策时钟声明改变")
    for field in ("source_hashes", "input_hashes", "prior_hashes"):
        for name, expected in frozen[field].items():
            if field == "input_hashes" and not with_inputs and name.startswith("research_inputs/"):
                continue
            require(file_sha256(ROOT / name) == expected, "冻结文件改变：" + name)
    return frozen


def freeze():
    require(not FREEZE.exists(), "决策时钟已冻结，不覆盖")
    plan = read_plan()
    require(not (ROOT / plan["output"]).exists(), "不能在有标签后冻结")
    old = json.loads((ROOT / "research/opportunity_quality_freeze.json").read_text())
    sources = dict(old["source_hashes"])
    for name in ("docs/study-decision-timing.py", "docs/verify-decision-timing.py",
                 "docs/verify-opportunity-quality.py", "tests/test_decision_timing.py"):
        sources[name] = file_sha256(ROOT / name)
    prior_names = subprocess.check_output(["git", "ls-files", "research_outputs"], cwd=ROOT, text=True).splitlines()
    prior_names += ["docs/opportunity-quality-results.md", "docs/opportunity-quality-evidence.json",
                    "research/opportunity_quality_plan.json", "research/opportunity_quality_freeze.json",
                    "research/implementation_freeze.json", "docs/publication_manifest.json"]
    overlay_path = ROOT / plan["execution_supplement"]["file"]
    overlay = json.loads(overlay_path.read_text())
    inputs = {str(overlay_path.relative_to(ROOT)): file_sha256(overlay_path)}
    for path, expected in overlay["source_hashes"].items():
        require(file_sha256(path) == expected, "补充官方资料改变")
        inputs[str(Path(path).relative_to(ROOT))] = expected
    write_json(FREEZE, {"schema": 1, "baseline_commit": plan["baseline_commit"],
                       "plan_sha256": file_sha256(PLAN), "source_hashes": sources,
                       "input_hashes": inputs, "prior_hashes": {p: file_sha256(ROOT / p) for p in prior_names}})
    print(json.dumps({"phase": "frozen", "sources": len(sources), "prior_files": len(prior_names)}), flush=True)


def make_segments(clocks, gap=30):
    """A live segment is determined from the prefix, including non-base candles."""
    segments, active = [], None
    for row in sorted(clocks, key=lambda r: (r["date"], r["contract"], r["time"])):
        identity = row["date"], row["contract"], row["direction"], row["period"]
        if active and (identity != active["identity"] or row["minute_index"] - active["last_base_index"] > gap):
            active = None
        if row["base_pass"]:
            if active is None:
                active = {"id": row["id"], "identity": identity, "base_id": row["id"], "strict_id": None,
                          "date": row["date"], "contract": row["contract"], "direction": row["direction"],
                          "product": row["product"], "group": row["group"], "rank_group": row["rank_group"],
                          "rank": row["rank"], "first_base_index": row["minute_index"],
                          "last_base_index": row["minute_index"], "base_clock_count": 0}
                segments.append(active)
            active["last_base_index"] = row["minute_index"]
            active["base_clock_count"] += 1
        row.update(segment_id=active["id"] if active else None, first_base=False, first_strict=False,
                   segment_age=row["minute_index"] - active["first_base_index"] if active else None)
        if active:
            row["first_base"] = row["id"] == active["base_id"]
            if row["strict_pass"] and active["strict_id"] is None:
                active["strict_id"] = row["id"]
                active["wait_trading_minutes"] = row["segment_age"]
                row["first_strict"] = True
    for segment in segments:
        del segment["identity"]
        if segment["strict_id"] is None:
            segment["wait_trading_minutes"] = None
    return segments


def phase_matches(clocks, rule):
    stages = defaultdict(list)
    for row in clocks:
        for stage, field in (("base", "first_base"), ("first_strict", "first_strict")):
            if row[field]:
                stages[(stage, row["date"], row["time"], row["group"], row["direction"], row["session_profile"])].append(row)
    pairs = []
    for key, rows in sorted(stages.items()):
        for treatment, control in rule["comparisons"]:
            used = set()
            for target in sorted((r for r in rows if r["rank_group"] == treatment), key=lambda r: (r["rank"], r["contract"])):
                options = []
                for other in rows:
                    if other["rank_group"] != control or other["id"] in used:
                        continue
                    age = abs(target["segment_age"] - other["segment_age"])
                    if age > 5 or min(target["previous_volume"], other["previous_volume"], target["relative_atr"], other["relative_atr"]) <= 0:
                        continue
                    volume = abs(math.log(target["previous_volume"] / other["previous_volume"]))
                    atr = abs(math.log(target["relative_atr"] / other["relative_atr"]))
                    if volume <= math.log(rule["maximum_liquidity_ratio"]) and atr <= math.log(rule["maximum_relative_atr_ratio"]):
                        options.append((volume + atr, other["contract"], other, volume, atr, age))
                if options:
                    distance, _, other, volume, atr, age = min(options, key=lambda o: (o[0], o[1]))
                    used.add(other["id"])
                    pairs.append({"stage": key[0], "comparison": treatment + "_vs_" + control,
                                  "date": target["date"], "time": target["time"], "treatment": target["id"],
                                  "control": other["id"], "segment_age_difference": age, "distance": distance,
                                  "liquidity_log_difference": volume, "relative_atr_log_difference": atr})
    return pairs


def scan_clocks(study, day, candidate):
    key, sign = candidate["contract"], 1 if candidate["direction"] == "LONG" else -1
    meta = study.data.metadata.get(key, day)
    schedule = study.minutes(day, key)
    bars = study.data.by_day.get((day, key), {})
    present = [t for t in schedule if t in bars]
    times = pd.DatetimeIndex([t + MINUTE for t in present])
    coverage = {"date": day, "contract": key, "scheduled": len(schedule), "missing": len(schedule) - len(present)}
    if not len(times):
        return [], coverage
    frames = {}
    for minutes in (1, 5, 15):
        source = study.features.frames.get((key, minutes))
        if source is None or source.empty:
            return [], coverage | {"indicator_unavailable": True}
        frames[minutes] = source.reindex(times, method="ffill" if minutes != 1 else None)
    one, five, fifteen = frames[1], frames[5], frames[15]
    opening = schedule[0]
    cutoff, _, _ = study.data.calendar.deadlines(day, meta, study.data.cfg["strategy"]["times"])
    ready = np.isfinite(one[["ma10", "ma20", "ma40", "previous_atr"]]).all(axis=1) & (one.previous_atr > 0)
    higher = (np.isfinite(five.ma20) & np.isfinite(fifteen[["ma10", "ma20", "ma20_slope3"]]).all(axis=1)
              & (fifteen.day == day) & (times >= opening + 15 * MINUTE)
              & (sign * (fifteen.ma10 - fifteen.ma20) > 0) & (sign * fifteen.ma20_slope3 > 0)
              & (sign * (fifteen.close - fifteen.ma20) > 0) & (sign * (five.close - five.ma20) > 0))
    indices = {t: i for i, t in enumerate(schedule)}
    records, found, previous, continuous = one.to_dict("records"), [], None, True
    for i, time in enumerate(present):
        bar, one_row, clock = bars[time], records[i], time + MINUTE
        position = indices[time]
        continuous = continuous and position == i
        if not ready.iloc[i]:
            previous = None
            continue
        if continuous and higher.iloc[i] and previous is not None and previous["end"] == time and clock < cutoff:
            result = study.logic.evaluate(bar, candidate, before_cutoff=True)
            flags = result["filters"]
            base = sign * (bar.close - one_row["ma20"]) > 0 and sign * (bar.close - previous["close"]) > 0
            strict = all(v for k, v in flags.items() if k not in {"candidate", "state", "entry_time"})
            found.append({"id": "minute/" + day + "/" + key + "/" + clock.isoformat(),
                          "date": day, "time": clock.isoformat(), "contract": key,
                          "product": candidate["product"], "group": candidate["group"],
                          "direction": candidate["direction"], "rank": candidate["rank"],
                          "rank_group": rank_group(candidate["rank"]), "session_profile": meta["session_profile"],
                          "period": study.data.calendar.locate(time, day, meta)[0].isoformat(),
                          "minute_index": position, "base_pass": bool(base), "strict_pass": bool(strict),
                          "previous_volume": candidate["previous_volume"],
                          "relative_atr": float(one_row["previous_atr"]) / bar.close,
                          "atr": float(one_row["previous_atr"]), "signal_price": bar.close,
                          "previous_close": float(previous["close"]), "ma20": float(one_row["ma20"]),
                          "filters": flags})
        previous = one_row | {"end": clock}
    return found, coverage | {"eligible_minute_clocks": len(found), "base_clocks": sum(r["base_pass"] for r in found),
                               "strict_clocks": sum(r["strict_pass"] for r in found)}


def execution_at_open(study, row):
    """Entry eligibility uses decision and next-open data, never horizon exits."""
    rejects, unknown = [], list(row["execution_rejections"])
    if row["cost_pass"] is False:
        rejects.append("signal_cost")
    if row["quantity_signal_empty"] == 0:
        rejects.append("signal_empty_account")
    schedule = study.minutes(row["date"], row["contract"])
    position = bisect.bisect_left(schedule, stamp(row["time"]))
    if position >= len(schedule):
        return {"status": "not_executable", "rejections": rejects + ["no_same_day_entry"], "unknown": unknown}
    time = schedule[position]
    bar = study.data.by_day[(row["date"], row["contract"])].get(time)
    result = {"entry_time": time.isoformat(), "rejections": rejects, "unknown": unknown}
    if bar is None:
        unknown.append("scheduled_entry_minute_missing")
    else:
        fill, reason = can_fill(bar)
        if not fill:
            rejects.append(reason)
        meta, reasons = study.parameters.resolve(row["contract"], time)
        unknown.extend(reasons)
        if meta is not None and not row["execution_rejections"]:
            sign, cfg = 1 if row["direction"] == "LONG" else -1, study.data.cfg
            s = cfg["strategy"]
            entry = slipped(bar.open, sign, meta, s["slippage_ticks"])
            if (meta["tick_size"], meta["value_per_price"]) != (row["signal_tick_size"], row["signal_value_per_price"]):
                unknown.append("contract_units_changed")
            else:
                fees = fee(meta, row["date"], "open", entry, 1) + fee(meta, row["date"], "close_today", entry, 1)
                cost = (fees / meta["value_per_price"] + 2 * s["slippage_ticks"] * meta["tick_size"]) / row["atr"]
                signal = {"snapshot": row["snapshot"], "time": row["time"], "direction": row["direction"]}
                guard = admissible_entry_price(entry_price_guard(signal, meta, s), entry)
                protection = scaled_protection(signal, meta, s, fees, row["protection"]["stop_loss_ticks"])
                quantity, _, _, risks = study.allocator.allocate(meta, entry, row["date"], {}, cfg["risk"]["initial_capital"],
                    maximum=row["quantity_signal_empty"], remaining_open_lots=meta.get("daily_open_limit"),
                    stop_loss_ticks=protection["stop_loss_ticks"])
                if cost > s["entry_cost_filter"]["max_cost_atr"] + 1e-12:
                    rejects.append("next_open_cost")
                if not guard:
                    rejects.append("next_open_price_guard")
                if quantity == 0:
                    rejects.append("next_open_empty_account")
                result.update(raw_open=bar.open, modeled_entry=entry, cost_at_open=cost, price_guard_pass=guard,
                              quantity_empty=quantity, account_rejections=risks,
                              initial_risk_distance=protection["stop_loss_ticks"] * meta["tick_size"])
    result["rejections"], result["unknown"] = sorted(set(rejects)), sorted(set(unknown))
    result["status"] = "not_executable" if rejects else "unknown" if unknown else "executable_empty_account"
    return result


def labels(study, row, horizons):
    old = study.plan
    study.plan = {"forward_labels": {"horizons": sorted(set(horizons))}}
    try:
        result = study.labels(row)
    finally:
        study.plan = old
    tick = study.data.metadata.get(row["contract"], row["date"])["tick_size"]
    for label in result.values():
        if label["raw_status"] == "complete":
            raw = label["raw_atr"] * row["atr"]
            label.update(raw_price=raw, raw_ticks=raw / tick)
            if label["economic_status"] == "complete":
                net = label["net_atr"] * row["atr"]
                label.update(net_price=net, net_ticks=net / tick, modeled_cost_price=raw - net)
    return result


def paired_labels(base, strict, wait, common=False):
    pairs = {}
    for h in (5, 15, 30):
        remaining = h - wait if common else h
        item = {"horizon": h, "raw_status": "censored", "economic_status": "unknown"}
        pairs[str(h)] = item
        if remaining <= 0:
            item["reason"] = "late_strict_no_remaining_minutes"
            continue
        a, b = base["labels"][str(h)], strict["labels"][str(remaining)]
        if a["raw_status"] != "complete" or b["raw_status"] != "complete":
            item["reason"] = "paired_raw_path_incomplete"
            continue
        if common:
            require(a["exit_time"] == b["exit_time"], "共同终点错配")
        raw = b["raw_price"] - a["raw_price"]
        item.update(raw_status="complete", base_entry_time=a["entry_time"], strict_entry_time=b["entry_time"],
                    base_exit_time=a["exit_time"], strict_exit_time=b["exit_time"],
                    raw_delta_price=raw, raw_delta_atr_base=raw / base["atr"],
                    raw_delta_bps_base=raw / a["raw_entry"] * 10000, raw_delta_ticks=raw / base["metadata_tick"])
        if a["economic_status"] == b["economic_status"] == "complete":
            net = b["net_price"] - a["net_price"]
            item.update(economic_status="complete", net_delta_price=net, net_delta_atr_base=net / base["atr"],
                        net_delta_bps_base=net / a["raw_entry"] * 10000,
                        net_delta_ticks=net / base["metadata_tick"],
                        raw_delta_same_economic_subset_atr_base=raw / base["atr"])
        else:
            item["reason"] = "paired_economic_label_unknown"
    return pairs


def supplementary(study, month):
    overlay = json.loads((ROOT / read_plan()["execution_supplement"]["file"]).read_text())
    rules = [r for r in overlay["rules"] if r["trading_day"].startswith(month)]
    cfg = copy.deepcopy(study.data.cfg)
    if rules:
        cfg["execution"]["qualification"]["rules"] += rules
        cfg["execution"]["qualification"]["supplier_specification_continuity_assumed"] = True
        cfg["execution"]["qualification_hash"] = digest(cfg["execution"]["qualification"])
    other = LabelStudy(study.data, study.features, study.plan)
    other.parameters = ExecutionParameters(cfg, study.data.metadata)
    return other


def old_stage_audit(rows, pairs):
    """Audit the frozen old pairs without selecting any replacement controls."""
    indexed = {r["id"]: r for r in rows}
    first = {r["segment_id"]: r for r in rows if r["representative"]}
    result = []
    for pair in pairs:
        a, b = indexed[pair["treatment"]], indexed[pair["control"]]
        ages = [r["minute_index"] - first[r["segment_id"]]["minute_index"] for r in (a, b)]
        result.append(pair | {"treatment_representative": a["representative"],
                             "control_representative": b["representative"],
                             "treatment_age": ages[0], "control_age": ages[1],
                             "segment_age_difference": abs(ages[0] - ages[1]),
                             "both_first": a["representative"] and b["representative"]})
    return result


def run_month(month):
    preserve(with_inputs=True)
    plan = read_plan()
    declaration = plan["months"][month]
    parent = ROOT / declaration["directory"]
    output = ROOT / plan["output"] / month
    require(not output.exists(), "窗口已有尝试；不覆盖或增加扫描")
    output.mkdir(parents=True)
    write_json(output / "attempt.json", {"status": "started", "month": month})
    cfg = read_config(parent / "config_snapshot.json")
    require(file_sha256(parent / "config_snapshot.json") == declaration["config_sha256"], "原配置改变")
    require(declaration["window"]["end"] < cfg["splits"]["test"]["start"], "不能读取锁定测试")
    print(json.dumps({"phase": "loading", "month": month}), flush=True)
    if month == "2026-09":
        data, features, evidence = prepare_review(ORIGINAL, cfg)
    else:
        data = restore_dataset(parent, cfg)
        require(data is not None and all(b.trading_day <= declaration["window"]["end"] for b in data.bars), "共享数据超出窗口")
        features = Features(data, cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (features.cache_key + ".jsonl.gz")
        evidence = {"source_run": str(parent), "cache": str(cache), "cache_key": features.cache_key,
                    "cache_sha256": file_sha256(cache), "data_fingerprint": data.fingerprint}
    study = LabelStudy(data, features, {"forward_labels": {"horizons": [5, 15, 30]}})
    study.parameters.preflight({m["product"] for m in cfg["metadata"]["contracts"]},
                              declaration["window"]["start"], declaration["window"]["end"], data)
    clocks, coverage, candidates = [], [], {}
    for day in declaration["trading_days"]:
        pool, pool_rejections = data.pool(day)
        opening = max(data.calendar.bounds(day, r["meta"])[0] + 8 * MINUTE for r in pool)
        ranks, ranking_rejections = rank_candidates(data, day, pool, opening, 2)
        coverage.append({"date": day, "pool_count": len(pool), "ranking_count": len(ranks),
                         "pool_rejections": pool_rejections, "ranking_rejections": ranking_rejections})
        for candidate in ranks:
            candidates[(day, candidate["contract"])] = candidate
            rows, count = scan_clocks(study, day, candidate)
            clocks.extend(rows)
            coverage.append(count)
        print(json.dumps({"phase": "clock_scan", "month": month, "date": day, "clocks": len(clocks)}), flush=True)
    segments = make_segments(clocks)
    matches = phase_matches(clocks, plan["matching"])
    by_clock = {r["id"]: r for r in clocks}
    wanted = {s[k] for s in segments for k in ("base_id", "strict_id") if s[k]}
    horizons = defaultdict(lambda: {5, 15, 30})
    for segment in segments:
        if segment["strict_id"]:
            horizons[segment["strict_id"]].update(h - segment["wait_trading_minutes"] for h in (5, 15, 30)
                                                 if h > segment["wait_trading_minutes"])
    supplemental_study, records, supplement_records = supplementary(study, month), {}, {}
    overlay = json.loads((ROOT / plan["execution_supplement"]["file"]).read_text())
    supplemented_keys = {(r["trading_day"], r["contract"]) for r in overlay["rules"]}
    for identity in sorted(wanted):
        clock = by_clock[identity]
        bar = data.by_day[(clock["date"], clock["contract"])][stamp(clock["time"]) - MINUTE]
        candidate = candidates[(clock["date"], clock["contract"])]
        row = study.observe(bar, candidate, clock["period"], source="minute")
        require(row is not None and row["id"] == identity, "时钟因果快照无法映射")
        row.update(metadata_tick=data.metadata.get(row["contract"], row["date"])["tick_size"],
                   segment_id=clock["segment_id"], segment_age=clock["segment_age"])
        row["entry_eligibility"] = execution_at_open(study, row)
        row["labels"] = labels(study, row, horizons[identity])
        records[identity] = row
        extra = row
        if (row["date"], row["contract"]) in supplemented_keys:
            extra = supplemental_study.observe(bar, candidate, clock["period"], source="minute")
            extra.update(metadata_tick=row["metadata_tick"], segment_id=row["segment_id"], segment_age=row["segment_age"])
            extra["entry_eligibility"] = execution_at_open(supplemental_study, extra)
            extra["labels"] = labels(supplemental_study, extra, horizons[identity])
        supplement_records[identity] = extra
    for segment in segments:
        strict = segment["strict_id"]
        for name, indexed in (("primary", records), ("supplementary", supplement_records)):
            status = "never_strict" if not strict else "strict_" + indexed[strict]["entry_eligibility"]["status"]
            segment[name + "_outcome"] = status
            if strict:
                segment[name + "_common_endpoint"] = paired_labels(indexed[segment["base_id"]], indexed[strict], segment["wait_trading_minutes"], True)
                segment[name + "_own_horizon"] = paired_labels(indexed[segment["base_id"]], indexed[strict], segment["wait_trading_minutes"], False)
    old_root = ROOT / plan["provenance"]["original_output"] / month
    old_rows = read_records(old_root / "observations.json.gz")
    old_pairs = read_records(old_root / "matches.json.gz")
    old_audit = old_stage_audit(old_rows, old_pairs)
    old_wanted = {p[k] for p in old_pairs for k in ("treatment", "control")}
    old_supplement = []
    for row in old_rows:
        if row["id"] not in old_wanted:
            continue
        extra = row
        if (row["date"], row["contract"]) in supplemented_keys:
            bar = data.by_day[(row["date"], row["contract"])][stamp(row["time"]) - MINUTE]
            candidate = candidates[(row["date"], row["contract"])]
            extra = supplemental_study.observe(bar, candidate, row["period"], source=row["source"])
            require(extra["id"] == row["id"], "旧配对时刻改变")
            extra.update(segment_id=row["segment_id"], representative=row["representative"],
                         metadata_tick=data.metadata.get(row["contract"], row["date"])["tick_size"])
            extra["labels"] = labels(supplemental_study, extra, (5, 15, 30))
        old_supplement.append(extra)
    # Preserve original portfolio state/trigger/fill information without replaying it.
    pointer = json.loads((ROOT / "research_outputs/rule_layers_2026-10-08" / (month + "_control_latest.json")).read_text())
    prior_run = Path(pointer["directory"])
    diagnostics = csv_rows(prior_run / "opportunity_diagnostics.csv.gz")
    published = {(r["date"], r["contract"], r["time"]): r for r in diagnostics if r["before_cutoff"] == "True"}
    events = {(r["date"], r["contract"], r["time"]): r for r in clocks}
    joins = []
    for key, original in sorted(published.items()):
        event = events.get(key)
        joins.append({"date": key[0], "contract": key[1], "time": key[2], "published": original,
                      "clock_id": event["id"] if event else None,
                      "segment_id": event["segment_id"] if event else None,
                      "first_strict": event["first_strict"] if event else False,
                      "base_pass": event["base_pass"] if event else None,
                      "strict_market_flag": event["strict_pass"] if event else None})
        if event:
            require(event["strict_pass"], "原严格行情条件不一致")
    for original in csv_rows(prior_run / "signals.csv.gz"):
        key = original["date"], original["contract"], original["time"]
        if key in published:
            published[key]["saved_trigger"] = original["trigger"]
            published[key]["saved_filled"] = original["filled"]
    actual_fills = defaultdict(list)
    for trade in csv_rows(prior_run / "trades.csv.gz"):
        key = stamp(trade["entry_signal_time"]).date().isoformat(), trade["contract"], trade["entry_signal_time"]
        actual_fills[key].append({k: trade[k] for k in ("id", "entry_signal_time", "entry_time", "entry_price",
                                                       "quantity", "direction", "exit_time", "net_pnl")})
    for join in joins:
        key = join["date"], join["contract"], join["time"]
        join["actual_fills"] = actual_fills.get(key, [])
        join["actual_fill_count"] = len(join["actual_fills"])
        published[key]["actual_fills"] = join["actual_fills"]
    for segment in segments:
        if segment["strict_id"]:
            row = records[segment["strict_id"]]
            segment["published_first_strict"] = published.get((row["date"], row["contract"], row["time"]))
    evidence.update(locked_test_read=False, original_config_sha256=file_sha256(parent / "config_snapshot.json"),
                    prior_signal_sha256=file_sha256(prior_run / "signals.csv.gz"),
                    prior_diagnostics_sha256=file_sha256(prior_run / "opportunity_diagnostics.csv.gz"),
                    prior_trade_sha256=file_sha256(prior_run / "trades.csv.gz"),
                    actual_prior_trade_count=sum(len(v) for v in actual_fills.values()))
    for name, rows in (("clocks", clocks), ("segments", segments), ("records", list(records.values())),
                       ("supplementary_records", list(supplement_records.values())), ("phase_matches", matches),
                       ("published_clock_joins", joins), ("prior_matching_stage_audit", old_audit),
                       ("prior_matching_supplementary_records", old_supplement)):
        write_gzip_json(output / (name + ".json.gz"), rows)
    write_json(output / "coverage.json", coverage)
    write_json(output / "sources.json", evidence)
    write_json(output / "attempt.json", {"status": "completed", "month": month})
    write_json(output / "manifest.json", {"status": "completed", "month": month,
        "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE),
        "counts": {"clocks": len(clocks), "segments": len(segments), "records": len(records), "phase_matches": len(matches),
                   "original_selected_segments": sum(s["rank_group"] == "rank_1_2" for s in segments)},
        "files": {p.name: file_sha256(p) for p in sorted(output.iterdir()) if p.is_file()},
        "full_strategy_backtests": 0, "locked_test_read": False})
    print(json.dumps({"phase": "completed", "month": month, "segments": len(segments), "matches": len(matches)}), flush=True)
    del data, features, study, supplemental_study
    gc.collect()


METRICS = ("raw_price", "raw_atr", "raw_bps", "raw_ticks", "net_price", "net_atr", "net_bps", "net_ticks", "mfe_atr", "mae_atr",
           "modeled_cost_price", "extra_tick_net_atr")


def summary(rows, h=15):
    raw = [r for r in rows if r["labels"][str(h)]["raw_status"] == "complete"]
    economic = [r for r in raw if r["labels"][str(h)]["economic_status"] == "complete"]
    return {"rows": len(rows), "contract_day_directions": len({(r["date"], r["contract"], r["direction"]) for r in rows}),
            "dates": len({r["date"] for r in rows}), "raw_complete": len(raw), "economic_complete": len(economic),
            "positive_net_rows": sum(r["labels"][str(h)]["net_atr"] > 0 for r in economic),
            "products": dict(Counter(r["product"] for r in rows)),
            "metrics": {f: estimate(daily_values(rows, f, h).values()) for f in METRICS},
            "gross_same_economic_subset": {f: estimate(daily_values(economic, f, h).values()) for f in ("raw_price", "raw_atr", "raw_bps", "raw_ticks")},
            "censor_reasons": dict(Counter(r["labels"][str(h)].get("reason") for r in rows
                                           if r["labels"][str(h)]["economic_status"] != "complete"))}


def pair_estimate(segments, name, h):
    field = name
    rows = [s | {"labels": s[field]} for s in segments if field in s]
    metrics = ("raw_delta_price", "raw_delta_atr_base", "raw_delta_bps_base", "raw_delta_ticks", "net_delta_price", "net_delta_atr_base", "net_delta_bps_base",
               "net_delta_ticks", "raw_delta_same_economic_subset_atr_base")
    return {"pairs": len(rows), "raw_complete": sum(r["labels"][str(h)]["raw_status"] == "complete" for r in rows),
            "economic_complete": sum(r["labels"][str(h)]["economic_status"] == "complete" for r in rows),
            "metrics": {f: estimate(daily_values(rows, f, h).values()) for f in metrics},
            "censor_reasons": dict(Counter(r["labels"][str(h)].get("reason") for r in rows
                                           if r["labels"][str(h)]["economic_status"] != "complete"))}


def concentration(rows, h=15):
    """Report product/date sensitivity; removing the best date is diagnostic only."""
    net_days = daily_values(rows, "net_atr", h)
    best = max(net_days, key=lambda day: (net_days[day], day)) if net_days else None
    product_summaries = {p: summary([r for r in rows if r["product"] == p], h)
                         for p in sorted({r["product"] for r in rows})}
    means = [s["metrics"]["net_atr"]["mean"] for s in product_summaries.values()
             if s["metrics"]["net_atr"]["mean"] is not None]
    return {"by_product": product_summaries, "net_atr_by_date": net_days,
            "positive_net_dates": sum(v > 0 for v in net_days.values()),
            "product_equal_net_atr": sum(means) / len(means) if means else None,
            "best_net_atr_date": best,
            "remove_best_date": summary([r for r in rows if r["date"] != best], h),
            "by_group": {g: summary([r for r in rows if r["group"] == g], h) for g in ("commodity", "financial")}}


def matched_summary(pairs, indexed, h):
    result = pair_summary(pairs, indexed, h)
    raw, economic = [], []
    for pair in pairs:
        a, b = indexed[pair["treatment"]], indexed[pair["control"]]
        x, y = a["labels"][str(h)], b["labels"][str(h)]
        raw_ok = x["raw_status"] == y["raw_status"] == "complete"
        net_ok = x["economic_status"] == y["economic_status"] == "complete"
        label = {"raw_status": "complete" if raw_ok else "censored", "economic_status": "complete" if net_ok else "unknown"}
        for field in ("raw_atr", "raw_bps", "raw_ticks", "net_atr", "net_bps", "net_ticks"):
            if field in x and field in y:
                label[field] = x[field] - y[field]
        row = a | {"labels": {str(h): label}}
        if raw_ok:
            raw.append(row)
        if net_ok:
            economic.append(row)
    result.update(raw_complete_pairs=len(raw),
                  metrics={f: estimate(daily_values(raw, f, h).values()) for f in ("raw_atr", "raw_bps", "raw_ticks", "net_atr", "net_bps", "net_ticks")},
                  gross_same_economic_subset={f: estimate(daily_values(economic, f, h).values()) for f in ("raw_atr", "raw_bps", "raw_ticks")})
    return result


def window_assessment(directory):
    segments, rows, extra, pairs, joins = [read_records(directory / (name + ".json.gz"))
                                         for name in ("segments", "records", "supplementary_records", "phase_matches", "published_clock_joins")]
    result = {}
    for mode, records in (("primary", rows), ("supplementary", extra)):
        indexed = {r["id"]: r for r in records}
        scopes = {}
        for name, subset in (("rank_1_2", [s for s in segments if s["rank_group"] == "rank_1_2"]), ("all_ranks", segments)):
            groups = {}
            for group_name, cohort in (("all", subset), ("eventually_strict", [s for s in subset if s["strict_id"]]),
                ("positive_wait", [s for s in subset if (s["wait_trading_minutes"] or 0) > 0]),
                ("zero_wait", [s for s in subset if s["wait_trading_minutes"] == 0]),
                ("never_strict", [s for s in subset if s[mode + "_outcome"] == "never_strict"]),
                ("strict_not_executable", [s for s in subset if s[mode + "_outcome"] == "strict_not_executable"]),
                ("strict_unknown", [s for s in subset if s[mode + "_outcome"] == "strict_unknown"]),
                ("strict_executable", [s for s in subset if s[mode + "_outcome"] == "strict_executable_empty_account"]),
                ("strict_executable_positive_wait", [s for s in subset if s[mode + "_outcome"] == "strict_executable_empty_account" and s["wait_trading_minutes"] > 0]),
                ("strict_executable_zero_wait", [s for s in subset if s[mode + "_outcome"] == "strict_executable_empty_account" and s["wait_trading_minutes"] == 0]),
                ("strict_executable_non_lc", [s for s in subset if s[mode + "_outcome"] == "strict_executable_empty_account" and s["product"].lower() != "lc"])):
                base = [indexed[s["base_id"]] for s in cohort]
                strict = [indexed[s["strict_id"]] for s in cohort if s["strict_id"]]
                groups[group_name] = {"segments": len(cohort), "outcomes": dict(Counter(s[mode + "_outcome"] for s in cohort)),
                    "base": {str(h): summary(base, h) for h in (5, 15, 30)},
                    "strict": {str(h): summary(strict, h) for h in (5, 15, 30)},
                    "common_endpoint": {str(h): pair_estimate(cohort, mode + "_common_endpoint", h) for h in (5, 15, 30)},
                    "own_horizon_difference": {str(h): pair_estimate(cohort, mode + "_own_horizon", h) for h in (5, 15, 30)}}
            groups["execution_rejection_counts"] = dict(Counter(reason for s in subset if s["strict_id"]
                for reason in indexed[s["strict_id"]]["entry_eligibility"]["rejections"]))
            groups["execution_unknown_counts"] = dict(Counter(reason for s in subset if s["strict_id"]
                for reason in indexed[s["strict_id"]]["entry_eligibility"]["unknown"]))
            executable = [indexed[s["strict_id"]] for s in subset if s[mode + "_outcome"] == "strict_executable_empty_account"]
            groups["executable_concentration"] = concentration(executable)
            scopes[name] = groups
        result[mode] = scopes
        result[mode + "_phase_matching"] = {stage: {str(h): {comparison: matched_summary(
            [p for p in pairs if p["stage"] == stage and p["comparison"] == comparison], indexed, h)
            for comparison in ("rank_1_2_vs_rank_3_5", "rank_1_2_vs_rank_6_plus", "rank_3_5_vs_rank_6_plus")}
            for h in (5, 15, 30)} for stage in ("base", "first_strict")}
    old_root = ROOT / read_plan()["provenance"]["original_output"] / directory.name
    audit = read_records(directory / "prior_matching_stage_audit.json.gz")
    old_sets = {"primary": read_records(old_root / "observations.json.gz"),
                "supplementary": read_records(directory / "prior_matching_supplementary_records.json.gz")}
    result["prior_matching_stage_audit"] = {"pairs": len(audit),
        "both_first_pairs": sum(p["both_first"] for p in audit),
        "nonrepresentative_controls": sum(not p["control_representative"] for p in audit),
        "control_age_minutes": dict(Counter(p["control_age"] for p in audit)),
        "age_difference_gt_5": sum(p["segment_age_difference"] > 5 for p in audit)}
    for mode, rset in old_sets.items():
        indexed = {r["id"]: r for r in rset}
        result["prior_matching_stage_audit"][mode] = {name: {str(h): {comparison: matched_summary(
            [p for p in cohort if p["comparison"] == comparison], indexed, h)
            for comparison in ("rank_1_2_vs_rank_3_5", "rank_1_2_vs_rank_6_plus", "rank_3_5_vs_rank_6_plus")}
            for h in (5, 15, 30)} for name, cohort in (("all_original_pairs", audit), ("both_first", [p for p in audit if p["both_first"]]))}
    result["published_clock_alignment"] = {"before_cutoff_cases": len(joins),
        "mapped_minute_clocks": sum(bool(j["clock_id"]) for j in joins),
        "outside_live_segments": sum(j["segment_id"] is None for j in joins),
        "first_strict_cases": sum(j["first_strict"] for j in joins),
        "strict_candles_without_base_continuation": sum(j["base_pass"] is False for j in joins),
        "actual_fills_joined": sum(j["actual_fill_count"] for j in joins),
        "actual_fills_at_first_strict": sum(j["actual_fill_count"] for j in joins if j["first_strict"]),
        "actual_fills_outside_segments": sum(j["actual_fill_count"] for j in joins if j["segment_id"] is None)}
    return result


def assess():
    frozen = preserve()
    plan = read_plan()
    root = ROOT / plan["output"]
    manifests, windows = {}, {}
    for month in plan["months"]:
        directory = root / month
        manifest = json.loads((directory / "manifest.json").read_text())
        require(manifest["status"] == "completed", "窗口未完成")
        for name, expected in manifest["files"].items():
            require(file_sha256(directory / name) == expected, "窗口标签改变：" + name)
        manifests[month] = file_sha256(directory / "manifest.json")
        windows[month] = window_assessment(directory)
    write_json(root / "assessment.json", {"status": "completed", "schema": 1,
        "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE),
        "sample_status": plan["sample_status"], "monthly_manifest_hashes": manifests, "windows": windows,
        "prior_files_preserved": len(frozen["prior_hashes"]), "full_strategy_backtests": 0,
        "locked_test_read": False, "automatic_promotion": False})
    with tarfile.open(root / "source_snapshot.tar.gz", "w:gz") as archive:
        for name in frozen["source_hashes"]:
            archive.add(ROOT / name, arcname=name)
        for path in (PLAN, FREEZE, ROOT / plan["execution_supplement"]["file"]):
            archive.add(path, arcname=str(path.relative_to(ROOT)))
    print(json.dumps({"phase": "assessed", "windows": list(windows), "prior_preserved": len(frozen["prior_hashes"])}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "assess"))
    parser.add_argument("--month", choices=("2026-07", "2026-08", "2026-09"))
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
    elif args.action == "assess":
        assess()
    else:
        require(args.month is not None, "run需要月份")
        run_month(args.month)


if __name__ == "__main__":
    main()
