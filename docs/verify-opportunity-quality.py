"""Verify published opportunity labels, optionally against archived raw prices."""

import argparse
import bisect
import csv
import gzip
import hashlib
import json
import math
import sys
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.calendar import MINUTE, Calendar, stamp  # noqa: E402
from research.data import Metadata  # noqa: E402
from research.execution_parameters import ExecutionParameters  # noqa: E402


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8), (actual, expected)


def charge(meta, day, side, price):
    applicable = [r for r in meta["fees"] if r["effective_from"] <= day and (not r.get("effective_to") or day <= r["effective_to"])]
    rule = max(applicable, key=lambda r: r["effective_from"])[side]
    return rule["value"] * (price * meta["value_per_price"] if rule["mode"] == "rate" else 1)


def quote(price, sign, meta, slippage):
    units = price / meta["tick_size"]
    rounded = math.ceil(units - 1e-9) if sign == 1 else math.floor(units + 1e-9)
    return round((rounded + sign * slippage) * meta["tick_size"], 10)


def capacity(meta, price, stop_ticks, cfg, maximum=None):
    s, r = cfg["strategy"], cfg["risk"]
    costs = charge(meta, meta["fees"][-1]["effective_from"], "open", price)
    costs += charge(meta, meta["fees"][-1]["effective_from"], "close_today", price)
    costs += 2 * s["slippage_ticks"] * meta["tick_size"] * meta["value_per_price"]
    loss = stop_ticks * meta["tick_size"] * meta["value_per_price"] + r["cost_buffer_multiple"] * costs
    margin = price * meta["value_per_price"] * meta["margin_rate"]
    fraction, capital = r["group_fractions"][meta["group"]], r["initial_capital"]
    caps = [math.floor(capital * r["trade_risk_fraction"] / loss), math.floor(capital * r["portfolio_risk_fraction"] / loss),
            math.floor(capital * r["portfolio_risk_fraction"] * fraction / loss), math.floor(capital * r["margin_fraction"] / margin),
            math.floor(capital * r["margin_fraction"] * fraction / margin), r["max_lots_per_contract"]]
    if maximum is not None:
        caps.append(maximum)
    if meta.get("daily_open_limit") is not None:
        caps.append(meta["daily_open_limit"])
    quantity = max(0, min(caps)) if r["max_positions"] > 0 else 0
    return quantity if quantity >= meta.get("min_open_lots", 1) else 0


def verify_month(root, month, declaration, with_data):
    directory = root / month
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    for filename, expected in manifest["files"].items():
        assert sha(directory / filename) == expected, filename
    observations, shapes, legacy, pairs = [read(directory / (name + ".json.gz")) for name in ("observations", "shapes", "legacy_cases", "matches")]
    indexed = {r["id"]: r for r in observations}
    assert len(indexed) == len(observations)
    prior, used = {}, set()
    for row in sorted(observations, key=lambda r: (r["date"], r["contract"], r["time"])):
        identity = row["date"], row["contract"], row["direction"], row["period"]
        old = prior.get(identity)
        first = old is None or row["minute_index"] - old["minute_index"] > 30
        assert row["representative"] == first
        assert row["segment_id"] == (row["id"] if first else old["segment_id"])
        prior[identity] = row
    for pair in pairs:
        a, b = indexed[pair["treatment"]], indexed[pair["control"]]
        assert all(a[k] == b[k] for k in ("date", "time", "group", "direction", "session_profile"))
        assert a["representative"]
        assert pair["comparison"] == a["rank_group"] + "_vs_" + b["rank_group"]
        assert abs(math.log(a["previous_volume"] / b["previous_volume"])) <= math.log(4) + 1e-12
        assert abs(math.log(a["relative_atr"] / b["relative_atr"])) <= math.log(2) + 1e-12
        use = pair["comparison"], pair["time"], pair["control"]
        assert use not in used
        used.add(use)
    report = {"month": month, "observations": len(observations), "segments": sum(r["representative"] for r in observations),
              "matched_pairs": len(pairs), "raw_prices_checked": False}
    if not with_data:
        return report
    cfg = json.loads((directory / "config_snapshot.json").read_text())
    metadata, calendar = Metadata(cfg["metadata"]), Calendar(cfg["calendar"])
    parameters = ExecutionParameters(cfg, metadata)
    parent = ROOT / declaration["directory"]
    with gzip.open(parent / "daily_candidates.csv.gz", "rt", encoding="utf-8-sig") as stream:
        candidates = {(r["date"], r["contract"]): r for r in csv.DictReader(stream)}
    all_rows = observations + shapes + legacy
    for row in all_rows:
        candidate = candidates[(row["date"], row["contract"])]
        assert row["rank"] == int(candidate["rank"]) and row["direction"] == candidate["direction"]
        close(row["r8"], float(candidate["r8"]))
    schedules, needed, contexts = {}, set(), {}
    for row in all_rows:
        day, key = row["date"], row["contract"]
        identity = day, key
        if identity not in schedules:
            schedules[identity] = calendar.minutes(day, metadata.get(key, day))
        times = schedules[identity]
        start = bisect.bisect_left(times, stamp(row["time"]))
        contexts[row["id"]] = start
        needed.add((day, key, row["time"]))
        for horizon in row["labels"]:
            for time in times[start:start + int(horizon)]:
                needed.add((day, key, (time + MINUTE).isoformat()))
    sources = json.loads((directory / "sources.json").read_text())
    source_run = Path(sources["source_run"])
    ref = json.loads((source_run / "data_reference.json").read_text())
    source = (source_run / ref["object"]).resolve()
    assert sha(source) == ref["sha256"]
    prices = {}
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        assert header["fingerprint"] == ref["base_fingerprint"]
        for line in stream:
            record = json.loads(line)
            raw = record["row"]
            assert raw["trading_day"] <= declaration["window"]["end"]
            if record["kind"] == "bar":
                identity = raw["trading_day"], raw["symbol"] + "." + raw["exchange"], (stamp(raw["datetime"]) + MINUTE).isoformat()
                if identity in needed:
                    prices[identity] = raw
    checked, economic, after_force = 0, 0, 0
    for row in all_rows:
        day, key, sign = row["date"], row["contract"], 1 if row["direction"] == "LONG" else -1
        assert stamp(row["time"]).date().isoformat() == day
        signal_bar = prices[(day, key, row["time"])]
        close(row["signal_price"], signal_bar["close"])
        meta_signal, _ = parameters.resolve(key, row["time"])
        if meta_signal:
            distance = (charge(meta_signal, day, "open", row["signal_price"]) + charge(meta_signal, day, "close_today", row["signal_price"])) / meta_signal["value_per_price"]
            distance += 2 * cfg["strategy"]["slippage_ticks"] * meta_signal["tick_size"]
            close(row["cost_atr"], distance / row["atr"])
            assert row["cost_pass"] == (distance / row["atr"] <= .5 + 1e-12)
            assert row["quantity_signal_empty"] == capacity(meta_signal, row["signal_price"], row["protection"]["stop_loss_ticks"], cfg)
        times, start = schedules[(day, key)], contexts[row["id"]]
        for horizon, label in row["labels"].items():
            path = [prices.get((day, key, (t + MINUTE).isoformat())) for t in times[start:start + int(horizon)]]
            if len(path) < int(horizon):
                assert label["raw_status"] == "censored" and label["reason"] == "day_ends_before_horizon"
                continue
            if any(b is None for b in path):
                assert label["raw_status"] == "censored" and label["reason"] == "scheduled_minute_missing"
                continue
            assert label["raw_status"] == "complete"
            assert stamp(label["entry_time"]) == times[start]
            assert stamp(label["exit_time"]) == times[start + int(horizon) - 1] + MINUTE
            entry, endpoint, atr = path[0]["open"], path[-1]["close"], row["atr"]
            close(label["raw_atr"], sign * (endpoint - entry) / atr)
            close(label["raw_bps"], sign * (endpoint - entry) / entry * 10000)
            close(label["mfe_atr"], max([0.] + [sign * (b["high" if sign == 1 else "low"] - entry) for b in path]) / atr)
            close(label["mae_atr"], max([0.] + [sign * (entry - b["low" if sign == 1 else "high"]) for b in path]) / atr)
            checked += 1
            _, force, _ = calendar.deadlines(day, metadata.get(key, day), cfg["strategy"]["times"])
            after_force += stamp(label["exit_time"]) > force
            if label["economic_status"] != "complete":
                assert "net_atr" not in label
                continue
            meta_entry, _ = parameters.resolve(key, label["entry_time"])
            meta_exit, _ = parameters.resolve(key, stamp(label["exit_time"]) - MINUTE / 1000000)
            assert meta_entry is not None and meta_exit is not None
            assert all(b.get("tradable", True) and b["volume"] > 0 for b in (path[0], path[-1]))
            slip = cfg["strategy"]["slippage_ticks"]
            buy, sell = quote(entry, sign, meta_entry, slip), quote(endpoint, -sign, meta_exit, slip)
            fees = charge(meta_entry, day, "open", buy) + charge(meta_exit, day, "close_today", sell)
            net = sign * (sell - buy) * meta_entry["value_per_price"] - fees
            close(label["net_per_lot_cny"], net)
            close(label["net_atr"], net / meta_entry["value_per_price"] / atr)
            extra_buy, extra_sell = quote(entry, sign, meta_entry, slip + 1), quote(endpoint, -sign, meta_exit, slip + 1)
            extra = sign * (extra_sell - extra_buy) * meta_entry["value_per_price"]
            extra -= charge(meta_entry, day, "open", extra_buy) + charge(meta_exit, day, "close_today", extra_sell)
            close(label["extra_tick_net_atr"], extra / meta_entry["value_per_price"] / atr)
            stop = label["initial_risk_distance"] / meta_entry["tick_size"]
            assert label["quantity_empty"] == capacity(meta_entry, buy, stop, cfg, row["quantity_signal_empty"])
            entry_fees = charge(meta_entry, day, "open", buy) + charge(meta_entry, day, "close_today", buy)
            entry_cost = (entry_fees / meta_entry["value_per_price"] + 2 * slip * meta_entry["tick_size"]) / atr
            close(label["cost_at_open"], entry_cost)
            tick = Decimal(str(meta_entry["tick_size"]))
            cap = Decimal(str(row["ma20"])) + sign * Decimal(str(cfg["strategy"]["extension_max"])) * Decimal(str(atr))
            cap = (cap / tick).to_integral_value(rounding=ROUND_FLOOR if sign == 1 else ROUND_CEILING) * tick
            guard = sign * (buy - float(cap)) <= float(tick) * 1e-8
            assert label["price_guard_pass"] == guard
            assert label["cost_pass_at_open"] == (entry_cost <= .5 + 1e-12)
            assert label["quantity_and_guard_feasible"] == bool(label["quantity_empty"] > 0 and guard and row["cost_pass"] and label["cost_pass_at_open"])
            economic += 1
    report.update(raw_prices_checked=True, raw_horizon_labels_checked=checked, economic_labels_checked=economic,
                  labels_ending_after_original_force_close=after_force,
                  source_sha256=ref["sha256"], rank_reference_sha256=sha(parent / "daily_candidates.csv.gz"))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-data", action="store_true")
    args = parser.parse_args()
    plan = json.loads((ROOT / "research/opportunity_quality_plan.json").read_text())
    frozen = json.loads((ROOT / "research/opportunity_quality_freeze.json").read_text())
    assert sha(ROOT / "research/opportunity_quality_plan.json") == frozen["plan_sha256"]
    for filename, expected in frozen["source_hashes"].items():
        assert sha(ROOT / filename) == expected, filename
    for filename, expected in frozen["prior_result_hashes"].items():
        assert sha(ROOT / filename) == expected, filename
    root = ROOT / plan["output"]
    report = {"status": "passed", "with_raw_data": args.with_data, "verifier_sha256": sha(__file__),
              "prior_result_files_verified": len(frozen["prior_result_hashes"]), "full_strategy_backtests": 0,
              "locked_test_read": False, "windows": []}
    for month, declaration in plan["months"].items():
        row = verify_month(root, month, declaration, args.with_data)
        report["windows"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    filename = "raw_label_verification.json" if args.with_data else "publication_verification.json"
    (root / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
