"""Independently check decision-clock selection and archived raw-price labels."""

import argparse
import bisect
import copy
import gzip
import hashlib
import importlib.util
import json
import math
import sys
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.calendar import MINUTE, Calendar, stamp  # noqa: E402
from research.config import digest  # noqa: E402
from research.data import Metadata  # noqa: E402
from research.exchange_diagnostic import PublicArchive, czce_parameters  # noqa: E402
from research.execution_parameters import ExecutionParameters  # noqa: E402

spec = importlib.util.spec_from_file_location("prior_raw_verifier", ROOT / "docs/verify-opportunity-quality.py")
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def records(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def near(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8), (actual, expected)


def verify_selection(directory):
    clocks, segments, rows, extras, matches = [records(directory / (n + ".json.gz"))
        for n in ("clocks", "segments", "records", "supplementary_records", "phase_matches")]
    by_clock, by_segment = {r["id"]: r for r in clocks}, {s["id"]: s for s in segments}
    assert len(by_clock) == len(clocks) and len(by_segment) == len(segments)
    histories = defaultdict(list)
    for r in clocks:
        strict = all(v for k, v in r["filters"].items() if k not in {"candidate", "state", "entry_time"})
        assert r["strict_pass"] == strict
        sign = 1 if r["direction"] == "LONG" else -1
        assert r["base_pass"] == (sign * (r["signal_price"] - r["ma20"]) > 0 and sign * (r["signal_price"] - r["previous_close"]) > 0)
        histories[(r["date"], r["contract"], r["direction"], r["period"])].append(r)
    for history in histories.values():
        base, last_base, strict = None, None, None
        for r in sorted(history, key=lambda r: r["minute_index"]):
            if last_base is not None and r["minute_index"] - last_base > 30:
                base, last_base, strict = None, None, None
            if r["base_pass"]:
                if base is None:
                    base = r
                last_base = r["minute_index"]
            if base is None:
                assert r["segment_id"] is None and not r["first_base"] and not r["first_strict"]
                continue
            assert r["segment_id"] == base["id"]
            assert r["segment_age"] == r["minute_index"] - base["minute_index"]
            assert r["first_base"] == (r["id"] == base["id"])
            first = strict is None and r["strict_pass"]
            assert r["first_strict"] == first
            if first:
                strict = r
                assert by_segment[base["id"]]["strict_id"] == r["id"]
        # Any strict observation in a segment must be its declared first strict.
    for s in segments:
        assigned = [r for r in histories[(s["date"], s["contract"], s["direction"], by_clock[s["base_id"]]["period"])]
                    if r["segment_id"] == s["id"]]
        strict = [r for r in assigned if r["strict_pass"]]
        expected = min(strict, key=lambda r: r["time"])["id"] if strict else None
        assert s["strict_id"] == expected
        assert s["base_clock_count"] == sum(r["base_pass"] for r in assigned)
        assert s["first_base_index"] == by_clock[s["base_id"]]["minute_index"]
        assert s["wait_trading_minutes"] == (by_clock[expected]["segment_age"] if expected else None)
    wanted = {s[k] for s in segments for k in ("base_id", "strict_id") if s[k]}
    for rset in (rows, extras):
        indexed = {r["id"]: r for r in rset}
        assert set(indexed) == wanted and len(indexed) == len(rset)
        for s in segments:
            mode = "primary" if rset is rows else "supplementary"
            expected = "never_strict" if s["strict_id"] is None else "strict_" + indexed[s["strict_id"]]["entry_eligibility"]["status"]
            assert s[mode + "_outcome"] == expected
            if s["strict_id"]:
                verify_pairs(s, indexed, mode)
    used = set()
    for pair in matches:
        a, b = by_clock[pair["treatment"]], by_clock[pair["control"]]
        assert all(a[k] == b[k] for k in ("date", "time", "group", "direction", "session_profile"))
        field = "first_base" if pair["stage"] == "base" else "first_strict"
        assert a[field] and b[field]
        assert pair["comparison"] == a["rank_group"] + "_vs_" + b["rank_group"]
        assert abs(a["segment_age"] - b["segment_age"]) <= 5
        assert abs(math.log(a["previous_volume"] / b["previous_volume"])) <= math.log(4) + 1e-12
        assert abs(math.log(a["relative_atr"] / b["relative_atr"])) <= math.log(2) + 1e-12
        key = pair["stage"], pair["comparison"], pair["time"], pair["control"]
        assert key not in used
        used.add(key)
    old_root = ROOT / "research_outputs/opportunity_quality_2026-10-09" / directory.name
    old_rows, old_pairs = [records(old_root / (n + ".json.gz")) for n in ("observations", "matches")]
    old_index = {r["id"]: r for r in old_rows}
    audit = records(directory / "prior_matching_stage_audit.json.gz")
    assert len(audit) == len(old_pairs)
    for checked, original in zip(audit, old_pairs, strict=True):
        assert all(checked[k] == v for k, v in original.items())
        a, b = old_index[original["treatment"]], old_index[original["control"]]
        assert checked["both_first"] == (a["representative"] and b["representative"])
        for label, r in (("treatment", a), ("control", b)):
            assert checked[label + "_representative"] == r["representative"]
            assert checked[label + "_age"] == r["minute_index"] - old_index[r["segment_id"]]["minute_index"]
        assert checked["segment_age_difference"] == abs(checked["treatment_age"] - checked["control_age"])
    old_extra = records(directory / "prior_matching_supplementary_records.json.gz")
    assert {r["id"] for r in old_extra} == {p[k] for p in old_pairs for k in ("treatment", "control")}
    for r in old_extra:
        assert all(r[k] == old_index[r["id"]][k] for k in ("date", "time", "contract", "direction", "rank", "segment_id", "representative"))
    joins = records(directory / "published_clock_joins.json.gz")
    for j in joins:
        assert j["actual_fill_count"] == len(j["actual_fills"])
        for fill in j["actual_fills"]:
            assert fill["entry_signal_time"] == j["time"]
    return {"clocks": len(clocks), "segments": len(segments), "phase_pairs": len(matches),
            "never_strict": sum(s["strict_id"] is None for s in segments), "old_pairs_stage_checked": len(audit),
            "actual_fills_joined": sum(j["actual_fill_count"] for j in joins)}


def verify_pairs(segment, indexed, mode):
    base, strict = indexed[segment["base_id"]], indexed[segment["strict_id"]]
    for kind in ("common_endpoint", "own_horizon"):
        for horizon, item in segment[mode + "_" + kind].items():
            h = int(horizon)
            remaining = h - segment["wait_trading_minutes"] if kind == "common_endpoint" else h
            if remaining <= 0:
                assert item["reason"] == "late_strict_no_remaining_minutes" and item["raw_status"] == "censored"
                continue
            a, b = base["labels"][horizon], strict["labels"][str(remaining)]
            if a["raw_status"] != "complete" or b["raw_status"] != "complete":
                assert item["raw_status"] == "censored"
                continue
            assert item["raw_status"] == "complete"
            if kind == "common_endpoint":
                assert a["exit_time"] == b["exit_time"]
            raw = b["raw_price"] - a["raw_price"]
            near(item["raw_delta_atr_base"], raw / base["atr"])
            near(item["raw_delta_bps_base"], raw / a["raw_entry"] * 10000)
            near(item["raw_delta_ticks"], raw / base["metadata_tick"])
            if a["economic_status"] == b["economic_status"] == "complete":
                assert item["economic_status"] == "complete"
                net = b["net_price"] - a["net_price"]
                near(item["net_delta_atr_base"], net / base["atr"])
                near(item["net_delta_bps_base"], net / a["raw_entry"] * 10000)
                near(item["net_delta_ticks"], net / base["metadata_tick"])
            else:
                assert item["economic_status"] != "complete" and "net_delta_atr_base" not in item


def raw_prices(directory, declaration, rows, cfg, calendar, metadata):
    schedules, needed = {}, set()
    for row in rows:
        identity = row["date"], row["contract"]
        if identity not in schedules:
            schedules[identity] = calendar.minutes(row["date"], metadata.get(row["contract"], row["date"]))
        schedule = schedules[identity]
        start = bisect.bisect_left(schedule, stamp(row["time"]))
        needed.add((*identity, row["time"]))
        needed.add((*identity, (stamp(row["time"]) - MINUTE).isoformat()))
        for horizon in row.get("labels", {}):
            for t in schedule[start:start + int(horizon)]:
                needed.add((*identity, (t + MINUTE).isoformat()))
    sources = json.loads((directory / "sources.json").read_text())
    source_run = Path(sources["source_run"])
    reference = json.loads((source_run / "data_reference.json").read_text())
    source = (source_run / reference["object"]).resolve()
    assert sha(source) == reference["sha256"]
    prices = {}
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        assert header["fingerprint"] == reference["base_fingerprint"]
        for line in stream:
            item = json.loads(line)
            raw = item["row"]
            assert raw["trading_day"] <= declaration["window"]["end"]
            if item["kind"] == "bar":
                identity = raw["trading_day"], raw["symbol"] + "." + raw["exchange"], (stamp(raw["datetime"]) + MINUTE).isoformat()
                if identity in needed:
                    prices[identity] = raw
    return prices, schedules, reference["sha256"]


def stop_floor(row, meta, cfg, fees, minimum=0):
    s, tick = cfg["strategy"], meta["tick_size"]
    scale = s["protection_scale"]
    return max(s["fixed_ticks"][meta["product"]]["stop_loss_ticks"], minimum,
               math.ceil(scale["atr_multiple"] * row["atr"] / tick - 1e-9),
               math.ceil(scale["roundtrip_cost_multiple"] * (fees / (tick * meta["value_per_price"]) + 2 * s["slippage_ticks"]) - 1e-9))


def independently_check_entry(row, prices, schedule, resolver, cfg):
    """Recompute refusals and unknowns using only the signal and next bar."""
    decision, rejects = row["entry_eligibility"], []
    unknown = list(row["execution_rejections"])
    if row["cost_pass"] is False:
        rejects.append("signal_cost")
    if row["quantity_signal_empty"] == 0:
        rejects.append("signal_empty_account")
    start = bisect.bisect_left(schedule, stamp(row["time"]))
    if start >= len(schedule):
        assert "no_same_day_entry" in decision["rejections"]
        rejects.append("no_same_day_entry")
    else:
        time = schedule[start]
        assert decision["entry_time"] == time.isoformat()
        bar = prices.get((row["date"], row["contract"], (time + MINUTE).isoformat()))
        if bar is None:
            unknown.append("scheduled_entry_minute_missing")
        else:
            if not bar.get("tradable", True):
                rejects.append("explicit_not_tradable")
            elif bar["volume"] <= 0:
                rejects.append("zero_volume")
            elif bar["high"] == bar["low"] and ((bar.get("limit_up") is not None and bar["high"] >= bar["limit_up"])
                     or (bar.get("limit_down") is not None and bar["low"] <= bar["limit_down"])):
                rejects.append("locked_limit")
            meta, reasons = resolver.resolve(row["contract"], time)
            unknown.extend(reasons)
            if meta is not None and not row["execution_rejections"]:
                sign = 1 if row["direction"] == "LONG" else -1
                entry = prior.quote(bar["open"], sign, meta, cfg["strategy"]["slippage_ticks"])
                if (meta["tick_size"], meta["value_per_price"]) != (row["signal_tick_size"], row["signal_value_per_price"]):
                    unknown.append("contract_units_changed")
                else:
                    fees = prior.charge(meta, row["date"], "open", entry) + prior.charge(meta, row["date"], "close_today", entry)
                    cost = (fees / meta["value_per_price"] + 2 * cfg["strategy"]["slippage_ticks"] * meta["tick_size"]) / row["atr"]
                    tick = Decimal(str(meta["tick_size"]))
                    boundary = Decimal(str(row["snapshot"]["ma20"])) + sign * Decimal(str(cfg["strategy"]["extension_max"])) * Decimal(str(row["atr"]))
                    rounding = ROUND_FLOOR if sign == 1 else ROUND_CEILING
                    limit = float((boundary / tick).to_integral_value(rounding=rounding) * tick)
                    guard = sign * (entry - limit) <= meta["tick_size"] * 1e-8
                    stop = stop_floor(row, meta, cfg, fees, row["protection"]["stop_loss_ticks"])
                    quantity = prior.capacity(meta, entry, stop, cfg, row["quantity_signal_empty"])
                    if cost > cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-12:
                        rejects.append("next_open_cost")
                    if not guard:
                        rejects.append("next_open_price_guard")
                    if quantity == 0:
                        rejects.append("next_open_empty_account")
                    near(decision["raw_open"], bar["open"])
                    near(decision["modeled_entry"], entry)
                    near(decision["cost_at_open"], cost)
                    near(decision["initial_risk_distance"], stop * meta["tick_size"])
                    assert decision["price_guard_pass"] == guard and decision["quantity_empty"] == quantity
    assert decision["rejections"] == sorted(set(rejects))
    assert decision["unknown"] == sorted(set(unknown))
    assert decision["status"] == ("not_executable" if rejects else "unknown" if unknown else "executable_empty_account")


def verify_raw(directory, declaration):
    parent = ROOT / declaration["directory"]
    cfg = json.loads((parent / "config_snapshot.json").read_text())
    assert sha(parent / "config_snapshot.json") == declaration["config_sha256"]
    metadata, calendar = Metadata(cfg["metadata"]), Calendar(cfg["calendar"])
    rows = records(directory / "records.json.gz")
    extra = records(directory / "supplementary_records.json.gz")
    old_extra = records(directory / "prior_matching_supplementary_records.json.gz")
    clocks = records(directory / "clocks.json.gz")
    prices, schedules, source_hash = raw_prices(directory, declaration, rows + old_extra + clocks, cfg, calendar, metadata)
    for row in clocks:
        identity = row["date"], row["contract"]
        near(row["signal_price"], prices[(*identity, row["time"])]["close"])
        near(row["previous_close"], prices[(*identity, (stamp(row["time"]) - MINUTE).isoformat())]["close"])
    supplemental_cfg = copy.deepcopy(cfg)
    overlay = json.loads((ROOT / "docs/decision-timing-execution-supplement.json").read_text())
    for path, expected in overlay["source_hashes"].items():
        assert sha(path) == expected
    supplement_checked = 0
    for rule in overlay["rules"]:
        if not rule["trading_day"].startswith(directory.name):
            continue
        source = Path(rule["sources"][0]["path"])
        text, proof = PublicArchive(source.parent).read(source.name, "www.czce.com.cn")
        assert proof["sha256"] == rule["sources"][0]["sha256"]
        parameters = czce_parameters(text, rule["source_date"])[rule["contract"].split(".")[0]]
        assert all(rule.get(k) == parameters.get(k) for k in ("fees", "margin_rate", "daily_open_limit"))
        assert rule["source_date"] == calendar.previous(rule["trading_day"])
        supplement_checked += 1
    q = supplemental_cfg["execution"]["qualification"]
    q["rules"] += [r for r in overlay["rules"] if r["trading_day"].startswith(directory.name)]
    q["supplier_specification_continuity_assumed"] = True
    supplemental_cfg["execution"]["qualification_hash"] = digest(q)
    # Exact original ranks and R8 are an independent archive, selected before labels.
    import csv
    with gzip.open(parent / "daily_candidates.csv.gz", "rt", encoding="utf-8-sig") as stream:
        candidates = {(r["date"], r["contract"]): r for r in csv.DictReader(stream)}
    report = {"source_sha256": source_hash, "rank_reference_sha256": sha(parent / "daily_candidates.csv.gz"),
              "raw_candle_clocks_checked": len(clocks), "indicator_reconstruction": False,
              "exact_official_supplement_rows_checked": supplement_checked}
    for mode, rset, configuration in (("primary", rows, cfg), ("supplementary", extra, supplemental_cfg),
                                      ("prior_matching_supplementary", old_extra, supplemental_cfg)):
        resolver = ExecutionParameters(configuration, metadata)
        raw_count, net_count, after_force = 0, 0, 0
        for row in rset:
            identity, sign = (row["date"], row["contract"]), 1 if row["direction"] == "LONG" else -1
            reference = candidates[identity]
            assert row["rank"] == int(reference["rank"]) and row["direction"] == reference["direction"]
            near(row["r8"], float(reference["r8"]))
            near(row["signal_price"], prices[(*identity, row["time"])]["close"])
            row.setdefault("metadata_tick", metadata.get(row["contract"], row["date"])["tick_size"])
            signal_meta, signal_rejects = resolver.resolve(row["contract"], row["time"])
            assert row["execution_rejections"] == signal_rejects
            if signal_meta:
                signal_fee = prior.charge(signal_meta, row["date"], "open", row["signal_price"])
                signal_fee += prior.charge(signal_meta, row["date"], "close_today", row["signal_price"])
                signal_cost = signal_fee / signal_meta["value_per_price"] + 2 * cfg["strategy"]["slippage_ticks"] * signal_meta["tick_size"]
                near(row["cost_atr"], signal_cost / row["atr"])
                assert row["cost_pass"] == (signal_cost / row["atr"] <= .5 + 1e-12)
                assert row["quantity_signal_empty"] == prior.capacity(signal_meta, row["signal_price"], row["protection"]["stop_loss_ticks"], cfg)
                assert row["protection"]["stop_loss_ticks"] == stop_floor(row, signal_meta, cfg, signal_fee)
                risk = cfg["risk"]
                per_risk = row["protection"]["stop_loss_ticks"] * signal_meta["tick_size"] * signal_meta["value_per_price"] + risk["cost_buffer_multiple"] * signal_cost * signal_meta["value_per_price"]
                per_margin = row["signal_price"] * signal_meta["value_per_price"] * signal_meta["margin_rate"]
                fraction = risk["group_fractions"].get(signal_meta["group"], 0)
                minimum = signal_meta.get("min_open_lots", 1)
                needs = [minimum * per_risk / f for f in (risk["trade_risk_fraction"], risk["portfolio_risk_fraction"], risk["portfolio_risk_fraction"] * fraction)]
                needs += [minimum * per_margin / f for f in (risk["margin_fraction"], risk["margin_fraction"] * fraction)]
                near(row["minimum_capital"], max(needs))
            schedule = schedules[identity]
            if "entry_eligibility" in row:
                independently_check_entry(row, prices, schedule, resolver, cfg)
            start = bisect.bisect_left(schedule, stamp(row["time"]))
            for h, label in row["labels"].items():
                path = [prices.get((*identity, (t + MINUTE).isoformat())) for t in schedule[start:start + int(h)]]
                if len(path) < int(h):
                    assert label["raw_status"] == "censored" and label["reason"] == "day_ends_before_horizon"
                    continue
                if any(b is None for b in path):
                    assert label["raw_status"] == "censored" and label["reason"] == "scheduled_minute_missing"
                    continue
                assert label["raw_status"] == "complete"
                assert label["entry_time"] == schedule[start].isoformat()
                assert stamp(label["exit_time"]) == schedule[start + int(h) - 1] + MINUTE
                entry, exit_price = path[0]["open"], path[-1]["close"]
                raw = sign * (exit_price - entry)
                if "raw_price" in label:
                    near(label["raw_price"], raw)
                    near(label["raw_ticks"], raw / row["metadata_tick"])
                near(label["raw_atr"], raw / row["atr"])
                near(label["raw_bps"], raw / entry * 10000)
                near(label["mfe_atr"], max([0.] + [sign * (b["high" if sign == 1 else "low"] - entry) for b in path]) / row["atr"])
                near(label["mae_atr"], max([0.] + [sign * (entry - b["low" if sign == 1 else "high"]) for b in path]) / row["atr"])
                raw_count += 1
                _, force, _ = calendar.deadlines(row["date"], metadata.get(row["contract"], row["date"]), cfg["strategy"]["times"])
                after_force += stamp(label["exit_time"]) > force
                if label["economic_status"] != "complete":
                    assert "net_atr" not in label
                    continue
                meta_entry, reasons = resolver.resolve(row["contract"], label["entry_time"])
                meta_exit, exit_reasons = resolver.resolve(row["contract"], stamp(label["exit_time"]) - MINUTE / 1000000)
                assert meta_entry and meta_exit and not signal_rejects and not reasons and not exit_reasons
                assert all(b.get("tradable", True) and b["volume"] > 0 for b in (path[0], path[-1]))
                slip = cfg["strategy"]["slippage_ticks"]
                x, y = prior.quote(entry, sign, meta_entry, slip), prior.quote(exit_price, -sign, meta_exit, slip)
                fees = prior.charge(meta_entry, row["date"], "open", x) + prior.charge(meta_exit, row["date"], "close_today", y)
                net = sign * (y - x) - fees / meta_entry["value_per_price"]
                if "net_price" in label:
                    near(label["net_price"], net)
                    near(label["net_ticks"], net / row["metadata_tick"])
                    near(label["modeled_cost_price"], raw - net)
                near(label["net_atr"], net / row["atr"])
                near(label["net_bps"], net / entry * 10000)
                x2, y2 = prior.quote(entry, sign, meta_entry, slip + 1), prior.quote(exit_price, -sign, meta_exit, slip + 1)
                net2 = sign * (y2 - x2) - (prior.charge(meta_entry, row["date"], "open", x2) + prior.charge(meta_exit, row["date"], "close_today", y2)) / meta_entry["value_per_price"]
                near(label["extra_tick_net_atr"], net2 / row["atr"])
                assert label["quantity_empty"] == prior.capacity(meta_entry, x, label["initial_risk_distance"] / meta_entry["tick_size"], cfg, row["quantity_signal_empty"])
                net_count += 1
        report[mode] = {"raw_horizon_labels_checked": raw_count, "economic_labels_checked": net_count,
                        "labels_ending_after_original_force_close": after_force,
                        "entry_decisions_independently_checked": sum("entry_eligibility" in r for r in rset)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-data", action="store_true")
    parser.add_argument("--month", choices=("2026-07", "2026-08", "2026-09"))
    args = parser.parse_args()
    plan = json.loads((ROOT / "docs/decision-timing-plan.json").read_text())
    frozen = json.loads((ROOT / "docs/decision-timing-freeze.json").read_text())
    assert sha(ROOT / "docs/decision-timing-plan.json") == frozen["plan_sha256"]
    for field in ("source_hashes", "prior_hashes", "input_hashes"):
        for path, expected in frozen[field].items():
            if field == "input_hashes" and path.startswith("research_inputs/") and not args.with_data:
                continue
            assert sha(ROOT / path) == expected, path
    root = ROOT / plan["output"]
    report = {"status": "passed", "with_raw_data": args.with_data, "verifier_sha256": sha(__file__),
              "prior_files_preserved": len(frozen["prior_hashes"]), "full_strategy_backtests": 0,
              "locked_test_read": False, "windows": []}
    for month in ([args.month] if args.month else list(plan["months"])):
        directory = root / month
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["status"] == "completed"
        assert manifest["plan_sha256"] == sha(ROOT / "docs/decision-timing-plan.json")
        assert manifest["freeze_sha256"] == sha(ROOT / "docs/decision-timing-freeze.json")
        for path, expected in manifest["files"].items():
            assert sha(directory / path) == expected, path
        window = verify_selection(directory) | {"month": month, "manifest_sha256": sha(directory / "manifest.json")}
        if args.with_data:
            window["raw_verification"] = verify_raw(directory, plan["months"][month])
        report["windows"].append(window)
        print(json.dumps(window, ensure_ascii=False), flush=True)
    filename = "raw_label_verification.json" if args.with_data else "publication_verification.json"
    target = root / args.month if args.month else root
    (target / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
