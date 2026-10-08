"""Read-only review of saved runs; recreate original indicator charts for latest fills."""

import csv
import gzip
import hashlib
import importlib.util
import io
import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

from research.calendar import Calendar
from research.feature_cache import read_frames
from research.storage import (
    BoundedFile,
    SpaceBudget,
    write_bounded_json,
    write_gzip_json,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "research_outputs/coverage_expansion_2026-10-05"
OUT = SOURCE / "ordered_opportunity/trade_review"
HERE = Path(__file__).parent
REASONS = {
    "fixed_stop": "初始止损",
    "breakeven_stop": "保本保护",
    "trailing_stop": "追踪保护",
    "volume": "放量走弱",
    "ma40_cross": "MA40反向穿越",
}
LABELS = {"combined": "最新组合版", "control": "原K=2对照"}
GROUPS = {
    "efficiency_035": ("效率下限0.45改为0.35", set()),
    "slope_1m": (
        "移除1分钟斜率范围及1跳下限",
        {"slope_1m_minimum", "slope_1m_maximum"},
    ),
    "slope_5m": (
        "移除5分钟斜率范围及1跳下限",
        {"slope_5m_minimum", "slope_5m_maximum"},
    ),
    "slope_both": (
        "同时移除两周期斜率范围及1跳下限",
        {
            "slope_1m_minimum",
            "slope_1m_maximum",
            "slope_5m_minimum",
            "slope_5m_maximum",
        },
    ),
    "oi": ("移除开盘净增仓要求", {"oi"}),
    "efficiency": ("移除方向效率门槛", {"efficiency"}),
    "shock": ("移除异常波幅门槛", {"shock"}),
    "displacement": ("移除同向位移下限", {"trend_displacement"}),
    "extension": ("移除MA20乖离上限", {"extension"}),
    "five_trend": ("移除5分钟价格位置要求", {"trend_5m"}),
    "cost": ("移除成本/ATR上限", {"cost"}),
}


def read(path):
    path = Path(path)
    if not path.exists() and path.name == "review_data.json":
        with gzip.open(path.with_suffix(".json.gz"), "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def equal(a, b):
    return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-7)


def dump(path, value):
    budget = SpaceBudget(read(HERE / "ordered_opportunity_plan.json")["budget"])
    if path.name == "review_data.json":
        write_gzip_json(path.with_suffix(".json.gz"), value, budget)
        return
    write_bounded_json(path, value, budget)


def write_artifact(path, content):
    path = Path(path)
    budget = SpaceBudget(read(HERE / "ordered_opportunity_plan.json")["budget"])
    temporary = path.with_name(path.name + ".partial")
    require(not temporary.exists(), "An unfinished artifact already exists: " + str(temporary))
    budget.check(temporary, reserve=len(content))
    with temporary.open("xb") as raw:
        with io.BufferedWriter(BoundedFile(raw, budget, temporary), buffer_size=1024 * 1024) as stream:
            stream.write(content)
    temporary.replace(path)
    budget.check(path)


def local(t):
    return datetime.fromisoformat(t).replace(tzinfo=None).isoformat()


def eligibility_stats(items):
    by_key = defaultdict(list)
    for r in items:
        by_key[(r["date"], r["contract"], r["direction"])].append(r["time"])
    episodes = 0
    for times in by_key.values():
        last = None
        for t in sorted(times):
            dt = datetime.fromisoformat(t)
            if last is None or dt - last != timedelta(minutes=1):
                episodes += 1
            last = dt
    return {
        "observations": len(items),
        "contract_day_directions": len(by_key),
        "contiguous_spells": episodes,
    }


def display_snapshots(full, key, day, cfg, sign, raw_bars):
    """Recover display values from causal frames and real session bars, even unselected minutes."""
    meta = max((m for m in cfg["metadata"]["contracts"]
                if m["symbol"] + "." + m["exchange"] == key
                and m.get("effective_from", "") <= day
                and (not m.get("effective_to") or day <= m["effective_to"])),
               key=lambda m: m.get("effective_from", ""))
    cal = Calendar(cfg["calendar"])
    opening, _ = cal.bounds(day, meta)
    expected = cal.minutes(day, meta)
    source = sorted((datetime.fromisoformat(time), bar) for (contract, time), bar in raw_bars.items()
                    if contract == key and bar["trading_day"] == day
                    and datetime.fromisoformat(time) >= opening)
    first = raw_bars.get((key, opening.isoformat()))
    baseline = (first["session_open_oi"] if first.get("session_open_oi") is not None
                else first["open_interest"]) if first else None
    result, cursor, volume, typical, turnover, turnover_valid = {}, 0, 0, 0, 0, True
    def finite(v):
        return v is not None and math.isfinite(v)
    for r in full:
        if r["day"] != day:
            continue
        clock = datetime.fromisoformat(r["datetime"])
        while cursor < len(source) and source[cursor][0] <= clock:
            b = source[cursor][1]
            volume += b["volume"]
            typical += (b["high"] + b["low"] + b["close"]) / 3 * b["volume"]
            turnover_valid &= b["turnover"] is not None and (b["turnover"] > 0 or b["volume"] == 0)
            turnover += b["turnover"] or 0
            cursor += 1
        continuous = cursor == bisect_right(expected, clock)
        vwap = None
        if continuous and volume > 0:
            if cfg["strategy"]["approximate_vwap"]:
                vwap = typical / volume
            elif turnover_valid and meta.get("turnover_factor"):
                vwap = turnover / volume / meta["turnover_factor"]
        raw = raw_bars[(key, r["datetime"])]
        ready = all(finite(r[n]) for n in ("ma10", "ma20", "ma40", "previous_atr", "shock", "efficiency"))
        snapshot = {"ma" + str(m): r["ma" + str(m)] for m in (10, 20, 40)}
        snapshot.update(
            vwap=vwap,
            oi_delta=raw["open_interest"] - baseline if raw["open_interest"] is not None and baseline is not None else None,
            efficiency=sign * r["efficiency"] if finite(r["efficiency"]) else None,
            shock=r["shock"] if ready else None,
            atr_previous=r["previous_atr"] if ready else None,
            extension=sign * (r["close"] - r["ma20"]) / r["previous_atr"] if ready and r["previous_atr"] > 0 else None,
            vr=r["vr"] if finite(r["vr"]) else None,
        )
        result[r["end"]] = snapshot
    return result


def collect(
    *, report=None, coverage=None, labels=None, focus="combined", diagnose=True
):
    OUT.mkdir(exist_ok=True)
    report = (
        report if report is not None else read(SOURCE / "optimization_report_data.json")
    )
    coverage = coverage if coverage is not None else read(SOURCE / "report_data.json")
    labels = labels if labels is not None else LABELS
    plan = read(HERE / "ordered_opportunity_plan.json")
    SpaceBudget(plan["budget"]).check(OUT)
    scenarios = [s for s in report["scenarios"] if s["variant"] in labels]
    scenarios.sort(key=lambda s: (s["variant"] != focus, s["month"], s["variant"]))
    months = sorted({s["month"] for s in scenarios})
    sources, observations, caches, raw_bars, frames = [], {}, {}, {}, {}
    summary, blocked, alternative = {}, [], defaultdict(list)
    current_eligible, gate_counts = [], Counter()
    gate_order = [
        ("候选分钟", []),
        (
            "时段、预热及数据连续",
            [
                "warmup_1m",
                "warmup_higher",
                "current_session_15m",
                "entry_time",
                "session_data_continuous",
            ],
        ),
        ("1/5/15分钟趋势", ["trend_1m", "trend_5m", "trend_15m"]),
        ("近似均价位置", ["vwap"]),
        ("开盘净增仓", ["oi"]),
        ("效率、异常波幅及乖离", ["efficiency", "shock", "extension"]),
        ("活跃度及同向位移", ["trend_activity", "trend_displacement"]),
        (
            "1/5分钟斜率范围",
            [
                "slope_1m_ready",
                "slope_1m_minimum",
                "slope_1m_maximum",
                "slope_5m_ready",
                "slope_5m_minimum",
                "slope_5m_maximum",
            ],
        ),
        ("无即时退场否决", ["no_exit_condition"]),
        ("执行资料可用", ["EXECUTION"]),
        ("成本/ATR≤0.5", ["cost"]),
    ]
    for s in scenarios:
        run = Path(s["directory"])
        for name, expected in s["source_hashes"].items():
            require(sha(run / name) == expected, "Source changed: " + str(run / name))
        require(s["audit_status"] == "passed", "Unaudited run")
        trades = list(rows(run / "trades.csv.gz"))
        cfg = read(run / "config_snapshot.json")
        require(len(trades) == s["metrics"]["trade_count"], "Count mismatch")
        require(
            equal(sum(float(t["net_pnl"]) for t in trades), s["metrics"]["net_profit"]),
            "PnL mismatch",
        )
        needed = {(t["contract"], t["entry_time"][:10]) for t in trades}
        obs = {}
        n_selected = 0
        for r in rows(run / "signals.csv.gz"):
            f = json.loads(r["filters"])
            if not f["candidate"]:
                continue
            n_selected += 1
            if (r["contract"], r["date"]) in needed:
                obs[(r["contract"], r["time"])] = {
                    **r,
                    "filters": f,
                    "snapshot": json.loads(r["snapshot"]),
                }
            if s["variant"] != focus or not diagnose:
                continue
            eligible = {k: v for k, v in f.items() if k != "state"}
            if r["execution_pass"] == "True" and all(eligible.values()):
                current_eligible.append(r)
            for key, (_, removed) in GROUPS.items():
                remaining = {k: v for k, v in eligible.items() if k not in removed}
                if key == "efficiency_035":
                    efficiency = json.loads(r["snapshot"]).get("efficiency")
                    remaining["efficiency"] = (
                        efficiency is not None and efficiency >= 0.35
                    )
                if r["execution_pass"] == "True" and all(remaining.values()):
                    alternative[key].append(
                        {k: r[k] for k in ["contract", "date", "direction", "time"]}
                    )
            passed = True
            for name, fields in gate_order:
                passed = passed and all(
                    (r["execution_pass"] == "True" if k == "EXECUTION" else f[k])
                    for k in fields
                )
                if passed:
                    gate_counts[name] += 1
            if r["trigger"] == "True" and r["risk_pass"] != "True":
                blocked.append(
                    {
                        k: r[k]
                        for k in [
                            "contract",
                            "date",
                            "direction",
                            "time",
                            "risk_rejections",
                        ]
                    }
                )
        observations[(s["month"], s["variant"])] = obs
        summary[(s["month"], s["variant"])] = {
            "run": str(run),
            "trades": trades,
            "cfg": cfg,
            "scenario": s,
        }
        sources.append(
            {
                "month": s["month"],
                "variant": s["variant"],
                "directory": str(run),
                "hashes": {
                    **s["source_hashes"],
                    "signals.csv.gz": sha(run / "signals.csv.gz"),
                    "events.csv.gz": sha(run / "events.csv.gz"),
                },
            }
        )
        require(
            n_selected == s["diagnostics"]["selected_observations"],
            "Candidate scope mismatch",
        )
        print("Signals checked", s["month"], s["variant"], n_selected, flush=True)
    for month in months:
        bundle = summary[(month, "control")]
        run = Path(bundle["run"])
        cfg = bundle["cfg"]
        needed = {
            (t["contract"], t["entry_time"][:10])
            for s in scenarios
            if s["month"] == month
            for t in s["trades"]
        }
        ref = read(run / "data_reference.json")
        source = (run / ref["object"]).resolve()
        require(sha(source) == ref["sha256"], "Market fingerprint mismatch")
        with gzip.open(source, "rt") as f:
            next(f)
            for line in f:
                item = json.loads(line)
                if item["kind"] != "bar":
                    continue
                bar = item["row"]
                require(bar["trading_day"] < "2026-09-24", "Locked test encountered")
                key = bar["symbol"] + "." + bar["exchange"]
                if (key, bar["trading_day"]) in needed:
                    raw_bars[(key, bar["datetime"])] = bar
        evidence = read(run / "prepared_source_review.json")
        cache = Path(evidence["cache"])
        require(
            sha(cache) == evidence["cache_sha256"], "Indicator fingerprint mismatch"
        )
        caches[month] = {
            "cache": str(cache),
            "cache_sha256": evidence["cache_sha256"],
            "market": str(source),
            "market_sha256": ref["sha256"],
        }
        for key, minutes, records in read_frames(cache, evidence["cache_key"]):
            if not any(k == key for k, _ in needed):
                continue
            require(
                all(r["day"] < "2026-09-24" for r in records),
                "Locked test in indicator cache",
            )
            frames[(month, key, minutes)] = records
        print("Market and indicators checked", month, flush=True)
    charts, checks = [], []
    for s in scenarios:
        b = summary[(s["month"], s["variant"])]
        cfg, trades = b["cfg"], b["trades"]
        obs = observations[(s["month"], s["variant"])]
        events = list(rows(Path(b["run"]) / "events.csv.gz"))
        for raw in trades:
            t = {
                k: raw[k]
                for k in [
                    "contract",
                    "direction",
                    "entry_time",
                    "exit_time",
                    "entry_signal_time",
                    "exit_signal_time",
                    "exit_reason",
                    "gap",
                ]
            }
            for k in [
                "entry_price",
                "exit_price",
                "stop_price",
                "target_price",
                "net_pnl",
                "fees",
                "gross_pnl",
                "holding_minutes",
            ]:
                t[k] = float(raw[k])
            for k in ["id", "quantity", "rank"]:
                t[k] = int(raw[k])
            t.update(
                month=s["month"],
                variant=s["variant"],
                label=labels[s["variant"]],
                uid=f"{s['variant']}-{s['month']}-{t['id']}",
            )
            t["entry_snapshot"] = json.loads(raw["entry_snapshot"])
            t["pullback"] = json.loads(raw["pullback"]) if raw["pullback"] else None
            t["entry_protection"] = json.loads(raw["entry_protection"])
            t["trailing_exit"] = json.loads(raw["trailing_exit"])
            t["entry_allocation"] = json.loads(raw["entry_allocation"])
            signal = obs[(t["contract"], t["entry_signal_time"])]
            require(
                signal["snapshot"] == t["entry_snapshot"], "Entry snapshot mismatch"
            )
            require(
                all(signal["filters"].values())
                and signal["filled"]
                == signal["risk_pass"]
                == signal["execution_pass"]
                == "True",
                "Entry gates not satisfied",
            )
            t["entry_price_check"] = json.loads(signal["fill_price_check"])
            rules = cfg["execution"]["qualification"]["rules"]
            rule = next(
                r
                for r in rules
                if r["contract"] == t["contract"]
                and r["trading_day"] == t["entry_time"][:10]
            )
            tick, value, sign = (
                rule["tick_size"],
                rule["value_per_price"],
                1 if t["direction"] == "LONG" else -1,
            )
            t["tick_size"] = tick
            t["entry_raw_quote"] = raw_bars[(t["contract"], t["entry_time"])]["open"]
            t["exit_raw_quote"] = t["exit_price"] + sign * tick
            require(
                equal(t["entry_price"], t["entry_raw_quote"] + sign * tick),
                "Entry slippage mismatch",
            )
            require(
                equal(
                    t["net_pnl"],
                    sign * (t["exit_price"] - t["entry_price"]) * value * t["quantity"]
                    - t["fees"],
                ),
                "Trade arithmetic mismatch",
            )
            for stage in ("entry", "exit"):
                hits = [
                    e
                    for e in events
                    if e["action"] == stage + "_filled"
                    and e["contract"] == t["contract"]
                    and e["time"] == t[stage + "_time"]
                    and equal(e["price"], t[stage + "_price"])
                    and int(e["quantity"]) == t["quantity"]
                ]
                require(len(hits) == 1, "Fill event mismatch")
            p = next(
                p
                for p in s["paths"]
                if p["contract"] == t["contract"] and p["entry_time"] == t["entry_time"]
            )
            for step in p["path"]:
                bar = raw_bars[(t["contract"], step["start"])]
                require(
                    all(
                        equal(step[k], bar[k]) for k in ["open", "high", "low", "close"]
                    ),
                    "Path candle mismatch",
                )
                require(
                    step["known_at_open"] <= step["start"],
                    "Protection uses future info",
                )
            if p["exit_kind"] == "intrabar_existing_line":
                step = p["path"][-1]
                require(
                    equal(t["exit_raw_quote"], step["stop_at_open"])
                    and step["low"] <= step["stop_at_open"] <= step["high"],
                    "Protective exit mismatch",
                )
            else:
                require(
                    equal(
                        t["exit_raw_quote"],
                        raw_bars[(t["contract"], t["exit_time"])]["open"],
                    ),
                    "Opening exit mismatch",
                )
            full = frames[(s["month"], t["contract"], 1)]
            displays = display_snapshots(full, t["contract"], t["entry_time"][:10], cfg, sign, raw_bars)
            five_rows = frames[(s["month"], t["contract"], 5)]
            five_ends = [r["end"] for r in five_rows]
            day_rows = []
            for r in full:
                if r["day"] != t["entry_time"][:10]:
                    continue
                raw_bar = raw_bars[(t["contract"], r["datetime"])]
                require(
                    all(
                        equal(r[k], raw_bar[k])
                        for k in ["open", "high", "low", "close", "volume"]
                    ),
                    "Chart candle differs from raw history",
                )
                saved = obs.get((t["contract"], r["end"]))
                snapshot = displays[r["end"]].copy()
                if saved:
                    for k, v in snapshot.items():
                        actual = saved["snapshot"].get(k)
                        require((actual is None and v is None) or
                                (actual is not None and v is not None and equal(actual, v)),
                                "Restored display measurement mismatch: " + k)
                    snapshot.update(saved["snapshot"])
                j = bisect_right(five_ends, r["end"]) - 1
                v = five_rows[j]["efficiency"] if j >= 0 else None
                snapshot["efficiency_5m"] = sign * v if v is not None and math.isfinite(v) else None
                snapshot["efficiency_5m_source_end"] = five_ends[j] if j >= 0 else None
                day_rows.append(
                    {
                        "start": r["datetime"],
                        "end": r["end"],
                        **{k: r[k] for k in ["open", "high", "low", "close", "volume"]},
                        "snapshot": snapshot,
                    }
                )
            context = {}
            for period in [1, 5, 15]:
                f = frames[(s["month"], t["contract"], period)]
                i = bisect_right([r["end"] for r in f], t["entry_signal_time"]) - 1
                require(i >= 22, "Warmup missing")
                for m in [10, 20]:
                    require(
                        equal(
                            sum(r["close"] for r in f[i - m + 1 : i + 1]) / m,
                            f[i]["ma" + str(m)],
                        ),
                        "MA independently recomputed mismatch",
                    )
                if period in [1, 5]:
                    sb = t["entry_snapshot"]["slope_band"][str(period) + "m"]
                    h = sb["lookback_bars"]
                    signed = (
                        sign
                        * (f[i]["ma20"] - f[i - h]["ma20"])
                        / (h * f[i]["previous_atr"])
                    )
                    require(
                        equal(signed, sb["signed_atr_per_bar"]),
                        "Slope arithmetic mismatch",
                    )
                    if period == 5 or t["entry_snapshot"].get("entry_channel") != "pullback":
                        require(
                            sb["min_atr_per_bar"] - 1e-12
                            <= signed
                            <= sb["max_atr_per_bar"] + 1e-12
                            and sign * (f[i]["ma20"] - f[i - h]["ma20"]) / tick >= 1 - 1e-8,
                            "Slope rule violated",
                        )
                    require(
                        all(
                            sign * (f[i]["close"] - f[i]["ma" + str(m)]) > 0
                            for m in [10, 20]
                        ),
                        "Trend price position mismatch",
                    )
                else:
                    require(
                        sign * (f[i]["ma10"] - f[i]["ma20"]) > 0
                        and sign * (f[i]["ma20"] - f[i - 3]["ma20"]) > 0
                        and sign * (f[i]["close"] - f[i]["ma20"]) > 0,
                        "15-minute trend mismatch",
                    )
                if period in [5, 15]:
                    context[str(period)] = f[max(0, i - 54) : i + 1]
            end_lookup = {r["end"]: r for r in day_rows}
            if t["exit_reason"] == "volume":
                last = end_lookup[t["exit_signal_time"]]
                prev = day_rows[day_rows.index(last) - 1]
                require(
                    last["snapshot"]["vr"] >= 2.5
                    and sign * (last["close"] - last["open"]) < 0
                    and sign * (last["close"] - prev["close"]) < 0,
                    "Volume weakness not satisfied",
                )
            if t["exit_reason"] == "ma40_cross":
                last = end_lookup[t["exit_signal_time"]]
                require(
                    sign * (last["close"] - last["snapshot"]["ma40"]) <= 0,
                    "MA40 exit not satisfied",
                )
                if cfg["strategy"].get("ma40_exit_confirmation_bars") == 2:
                    previous = day_rows[day_rows.index(last) - 1]
                    require(previous["end"] == last["start"] and
                            sign * (previous["close"] - previous["snapshot"]["ma40"]) <= 0,
                            "MA40 adjacent two-bar confirmation missing")
            t["cost_atr"] = (
                t["entry_protection"]["roundtrip_fee_and_slippage_ticks"]
                * tick
                / t["entry_protection"]["signal_atr"]
            )
            if cfg["strategy"].get("entry_cost_filter"):
                require(
                    t["cost_atr"]
                    <= cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-8,
                    "Cost limit violated",
                )
            t["wall_minutes"] = (
                datetime.fromisoformat(t["exit_time"])
                - datetime.fromisoformat(t["entry_time"])
            ).total_seconds() / 60
            entry_i = next(
                i for i, r in enumerate(day_rows) if r["start"] == t["entry_time"]
            )
            exit_start = p["path"][-1]["start"]
            exit_i = next(i for i, r in enumerate(day_rows) if r["start"] == exit_start)
            local_rows = day_rows[max(0, entry_i - 25) : exit_i + 16]
            t["entry_channel"] = t["entry_snapshot"].get("entry_channel", "direct")
            display_strategy = {k: v for k, v in cfg["strategy"].items() if k != "fixed_ticks"}
            if t["entry_channel"] != "pullback":
                display_strategy.pop("trend_entry", None)
            charts.append(
                {
                    "trade": t,
                    "path": p,
                    "rows": day_rows,
                    "local_rows": local_rows,
                    "higher": context,
                    "strategy": display_strategy,
                }
            )
            checks.append(
                {
                    "uid": t["uid"],
                    "all_entry_filters": True,
                    "entry_and_exit_events": True,
                    "raw_candles": True,
                    "independent_MA_and_slopes": True,
                    "exit_rule_and_price": True,
                    "known_protection_only": True,
                    "missing_selection_minutes_restored_from_causal_sources": True,
                    "net_pnl": t["net_pnl"],
                }
            )
    diagnostics = {
        "gates": [
            {"label": name, "observations": gate_counts[name]} for name, _ in gate_order
        ],
        "eligible": eligibility_stats(current_eligible),
        "risk_rejections": blocked,
        "risk_distinct": eligibility_stats(blocked),
        "alternatives": [
            {"key": key, "label": label, **eligibility_stats(alternative[key])}
            for key, (label, _) in GROUPS.items()
        ],
    }
    data = {
        "created": datetime.now().astimezone().isoformat(),
        "latest_saved_run_created": report["created"],
        "charts": charts,
        "totals": report["assessment"]["totals"],
        "labels": report["labels"],
        "coverage_totals": coverage["totals"],
        "windows": [
            {
                "month": s["month"],
                "variant": s["variant"],
                "metrics": s["metrics"],
                "funnel": s["funnel"],
            }
            for s in report["scenarios"]
        ],
        "research_days": report["research_days"],
        "sample_note": report["sample_note"],
        "diagnostics": diagnostics,
        "verification": {
            "status": "passed",
            "checks": checks,
            "sources": sources,
            "cache_sources": caches,
            "locked_test_read": False,
            "strategy_changed": False,
        },
    }
    dump(OUT / "review_data.json", data)
    dump(OUT / "verification.json", data["verification"])
    if diagnose:
        dump(OUT / "frequency_diagnosis.json", diagnostics)
    fields = [
        "variant",
        "month",
        "id",
        "contract",
        "direction",
        "entry_signal_time",
        "entry_time",
        "exit_signal_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "quantity",
        "exit_reason",
        "holding_minutes",
        "wall_minutes",
        "fees",
        "net_pnl",
        "cost_atr",
    ]
    with io.StringIO(newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(c["trade"] for c in charts)
        content = f.getvalue().encode("utf-8-sig")
    write_artifact(OUT / "trade_review.csv", content)
    print(
        json.dumps(
            {"diagnostics": diagnostics, "charts": len(charts)}, ensure_ascii=False
        ),
        flush=True,
    )
    return data


def higher_figure(c):
    fig = make_subplots(
        rows=2,
        cols=1,
        vertical_spacing=0.17,
        subplot_titles=["5分钟：入场前已完成K线", "15分钟：入场前已完成K线"],
    )
    for row, period in enumerate(["5", "15"], 1):
        bars = c["higher"][period]
        times = [local(r["end"]) for r in bars]
        fig.add_trace(
            go.Candlestick(
                x=times,
                open=[r["open"] for r in bars],
                high=[r["high"] for r in bars],
                low=[r["low"] for r in bars],
                close=[r["close"] for r in bars],
                name=period + "分钟K线",
                showlegend=False,
                increasing_line_color="#d95d58",
                decreasing_line_color="#259581",
            ),
            row=row,
            col=1,
        )
        for m, color in [(10, "#d99b13"), (20, "#6b5bd1"), (40, "#1e8caa")]:
            fig.add_trace(
                go.Scatter(
                    x=times,
                    y=[r["ma" + str(m)] for r in bars],
                    name="MA" + str(m),
                    mode="lines",
                    line=dict(color=color, width=1.8),
                    showlegend=row == 1,
                ),
                row=row,
                col=1,
            )
        last = bars[-1]
        fig.add_trace(
            go.Scatter(
                x=[times[-1]],
                y=[last["close"]],
                mode="markers",
                marker=dict(
                    symbol="diamond-open", size=13, color="#2563eb", line_width=2
                ),
                name="信号时最新完成K线",
                showlegend=row == 1,
            ),
            row=row,
            col=1,
        )
        fig.update_xaxes(
            type="category",
            tickmode="array",
            tickvals=times[::8] + [times[-1]],
            ticktext=[r[5:10] + " " + r[11:16] for r in times[::8] + [times[-1]]],
            rangeslider_visible=False,
            row=row,
            col=1,
        )
    fig.update_layout(
        template="plotly_white",
        height=680,
        margin=dict(t=75, b=55, l=75, r=35),
        legend=dict(orientation="h", y=1.12),
        font=dict(family="Noto Sans CJK SC, sans-serif", color="#344054"),
        hovermode="x unified",
    )
    return json.loads(fig.to_json())


def build(data, *, template_path=None, destination=None):
    spec = importlib.util.spec_from_file_location(
        "original_charts", ROOT / "research_inputs/2026-09/plot_trade_indicators.py"
    )
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    for c in data["charts"]:
        t, p = c["trade"], c["path"]
        c["views"] = {}
        for view, rs in [("local", c["local_rows"]), ("day", c["rows"])]:
            f, close_range = original.make_figure(t, rs, c["strategy"])
            for tr in f["data"]:
                if tr.get("name") == "方向效率" and c["strategy"].get("trend_entry"):
                    tr["name"] = "1分钟方向效率（仅展示）"
                if (
                    tr.get("name") == "开盘净增仓"
                    and not c["strategy"]["enable_oi_filter"]
                ):
                    tr["name"] = "开盘净增仓（仅展示）"
                if tr.get("name") == "固定止盈参考":
                    tr["name"] = "追踪启动价（原目标）"
                    tr["hovertemplate"] = (
                        "达到此价后启动追踪；不立即止盈 %{y:,.4f}<extra></extra>"
                    )
                if tr.get("name") == "固定止损参考":
                    tr["name"] = "初始止损"
                    tr["hovertemplate"] = (
                        "初始止损 %{y:,.4f}<extra>实际有效保护见红色阶梯线</extra>"
                    )
                if tr.get("name") in {"初始止损", "追踪启动价（原目标）"}:
                    tr["x"][1] = local(t["exit_time"])
            if c["strategy"].get("trend_entry"):
                for shape in f["layout"]["shapes"]:
                    if shape.get("line", {}).get("color") == "#447c65":
                        shape["line"]["color"] = "#2465a8"
                f["data"].append({"type": "scatter", "mode": "lines",
                    "x": [local(r["end"]) for r in rs],
                    "y": [r["snapshot"]["efficiency_5m"] for r in rs],
                    "customdata": [r["snapshot"]["efficiency_5m_source_end"] for r in rs],
                    "name": "5分钟方向效率（入场门槛）",
                    "line": {"color": "#2465a8", "width": 2, "shape": "hv"},
                    "xaxis": "x4", "yaxis": "y5",
                    "hovertemplate": "5分钟效率 %{y:.3f}<br>最新完成根 %{customdata}<extra></extra>"})
                touch = t["pullback"]
                r = next(row for row in rs if row["end"] == touch["event"])
                f["data"].append({"type": "scatter", "mode": "markers",
                    "x": [local(touch["event"])], "y": [r["low"] if t["direction"] == "LONG" else r["high"]],
                    "name": "已完成MA10回踩", "marker": {"symbol": "circle-open", "color": "#7a45bc", "size": 15, "line": {"width": 2}},
                    "xaxis": "x", "yaxis": "y"})
                qualification = t["entry_snapshot"]["trend_entry"]
                f["layout"]["shapes"].append({"type": "rect", "xref": "x", "yref": "y domain",
                    "x0": local(qualification["armed_at"]), "x1": local(qualification["expires_at"]),
                    "y0": 0, "y1": 1, "fillcolor": "rgba(122,69,188,0.06)", "line": {"width": 0}, "layer": "below"})
            x, y = [], []
            for step in p["path"]:
                end = min(step["end"], t["exit_time"])
                if end > step["start"]:
                    x += [local(step["start"]), local(end), None]
                    y += [step["stop_at_open"]] * 2 + [None]
            # The final marker makes an opening execution's existing protection visible.
            x += [local(t["exit_time"])]
            y += [p["final_stop"]]
            f["data"].append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": x,
                    "y": y,
                    "name": "当时生效的保护线",
                    "line": {"color": "#ad4233", "width": 2.7},
                    "hovertemplate": "当前保护 %{y:,.4f}<extra>只使用当时已知信息</extra>",
                    "xaxis": "x",
                    "yaxis": "y",
                }
            )
            be = t["trailing_exit"].get("breakeven_activation_price")
            if be is not None:
                f["data"].append(
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "x": [local(t["entry_time"]), local(t["exit_time"])],
                        "y": [be, be],
                        "name": f"{c['strategy']['breakeven']['activation_r']:g}R保本启动价",
                        "line": {"color": "#bd8a2e", "width": 1, "dash": "dot"},
                        "xaxis": "x",
                        "yaxis": "y",
                    }
                )
            f["layout"]["margin"]["t"] = 125
            f["layout"]["legend"].update(y=1.16, font={"size": 11})
            f["layout"]["height"] = 970
            for name in ["xaxis", "xaxis2", "xaxis3", "xaxis4"]:
                f["layout"][name]["hoverformat"] = "%H:%M"
            c["views"][view] = {
                "figure": f,
                "close_range": close_range,
                "bar_count": len(rs),
            }
        c["higher_figure"] = higher_figure(c)
        c.pop("rows")
        c.pop("local_rows")
        c.pop("higher")
    payload = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).replace("</", "<\\/")
    template = Path(
        template_path or HERE / "latest_trade_review.html.template"
    ).read_text()
    html = template.replace("__PLOTLY__", get_plotlyjs()).replace("__DATA__", payload)
    target = Path(destination or OUT / "latest_entry_exit_review.html")
    write_artifact(target, html.encode("utf-8"))
    print("Built", target, flush=True)


if __name__ == "__main__":
    import sys

    data = read(OUT / "review_data.json") if "--reuse" in sys.argv else collect()
    if "--collect-only" not in sys.argv:
        build(data)
