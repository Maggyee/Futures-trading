"""Independent journal arithmetic and causal re-entry audit for finite variants."""

import argparse
import itertools
import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from .coverage_audit import audit_run, rows
from .data import file_sha256
from .feature_cache import read_frames
from .optimization_audit import close, require
from .optimization_declaration import validate_optimization
from .reporting import write_json
from .storage import SpaceBudget


def audit_journal(run, cfg, fee_solver):
    plan, old = validate_optimization(cfg)
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    trades = list(rows(Path(run) / "trades.csv.gz"))
    stops = defaultdict(list)
    for t in trades:
        if t["exit_reason"] == "fixed_stop" and float(t["net_pnl"]) < 0:
            stops[(t["exit_time"][:10], t["contract"], t["direction"])].append(
                t["exit_time"]
            )
    rules = {
        (r["trading_day"], r["contract"]): r
        for r in cfg["execution"]["qualification"]["rules"]
    }
    fields = (
        "time",
        "date",
        "contract",
        "direction",
        "rank",
        "r8",
        "group",
        "product",
        "pullback",
        "exit_flags",
        "execution_pass",
        "execution_rejections",
    )
    count, state_changes, cost_checked, blocked = 0, 0, 0, 0
    changed = Counter()
    efficiency_checks = defaultdict(list)
    slope_checks = defaultdict(list)
    for left, right in itertools.zip_longest(
        rows(parent / "signals.csv.gz"), rows(Path(run) / "signals.csv.gz")
    ):
        require(left is not None and right is not None, "观察行数改变")
        require(all(left[k] == right[k] for k in fields), "候选/执行资料改变")
        before, after = (json.loads(r["filters"]) for r in (left, right))
        a, b = (json.loads(r["snapshot"]) for r in (left, right))
        expected = dict(before)
        value = b["efficiency"]
        expected["efficiency"] = (
            value is not None
            and math.isfinite(value)
            and value >= cfg["strategy"]["efficiency_min"]
        )
        expected["oi"] = (
            (b.get("oi_delta") is not None and b["oi_delta"] > 0)
            if cfg["strategy"]["enable_oi_filter"]
            else True
        )
        if expected["efficiency"] != before["efficiency"] or right["filled"] == "True":
            efficiency_checks[right["contract"]].append(
                (right["time"], right["direction"], value)
            )
        for label in ("1m", "5m"):
            rule = cfg["strategy"]["slope_band"]["timeframes"][label]
            for field in ("min_atr_per_bar", "max_atr_per_bar"):
                require(
                    b["slope_band"][label][field] == rule[field], "斜率声明阈值不符"
                )
                a["slope_band"][label][field] = rule[field]
            ready = before["slope_" + label + "_ready"]
            point = b["slope_band"][label]
            expected["slope_" + label + "_minimum"] = bool(
                ready
                and point["signed_atr_per_bar"] >= rule["min_atr_per_bar"] - 1e-12
                and point["signed_move_ticks"] >= point["min_move_ticks"] - 1e-8
            )
            expected["slope_" + label + "_maximum"] = bool(
                ready and point["signed_atr_per_bar"] <= rule["max_atr_per_bar"] + 1e-12
            )
            if any(
                expected["slope_" + label + "_" + k]
                != before["slope_" + label + "_" + k]
                for k in ("minimum", "maximum")
            ):
                slope_checks[(right["contract"], int(label[:-1]))].append(
                    (right["time"], right["direction"], point)
                )
        require(a == b, "复用改变了固有指标测量值")
        if right["execution_pass"] == "True":
            rule = rules[(right["date"], right["contract"])]
            check = json.loads(right["cost_check"])
            price = Decimal(str(check["price"]))
            atr = b["atr_previous"]
            costs = fee_solver(rule, 1, "open", price) + fee_solver(
                rule, 1, "close_today", price
            )
            distance = (
                float(costs) / rule["value_per_price"]
                + 2 * cfg["strategy"]["slippage_ticks"] * rule["tick_size"]
            )
            ratio = (
                distance / atr
                if atr is not None and math.isfinite(atr) and atr > 0
                else None
            )
            expected["cost"] = (
                ratio is not None
                and ratio
                <= cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-12
            )
            if ratio is not None:
                close(check["cost_atr"], ratio, "成本ATR")
            require(check["accepted"] == expected["cost"], "成本判断不符")
            cost_checked += 1
        else:
            expected["cost"] = False
        if cfg["strategy"].get("block_same_day_reentry_after_stop"):
            prior = stops.get(
                (right["date"], right["contract"], right["direction"]), []
            )
            expected["stop_reentry"] = not any(t <= right["time"] for t in prior)
            if not expected["stop_reentry"]:
                blocked += 1
                require(
                    right["trigger"] == right["filled"] == "False",
                    "止损后同方向仍然进场",
                )
        state_changes += before["state"] != after["state"]
        expected["state"] = after["state"]
        require(expected == after, "独立过滤不符")
        for k, v in after.items():
            changed[k] += before.get(k) != v
        require(right["all_pass"] == str(all(after.values())), "总通过标记不符")
        rejections = json.loads(right["rejections"])
        require(
            set(rejections) == {k for k, v in after.items() if not v}
            and len(rejections) == len(set(rejections)),
            "拒绝原因不完整",
        )
        count += 1
    evidence = json.loads((Path(run) / "prepared_source_review.json").read_text())
    require(
        file_sha256(evidence["cache"]) == evidence["cache_sha256"], "独立指标来源不符"
    )
    checks = Counter()
    for key, minutes, records in read_frames(evidence["cache"], evidence["cache_key"]):
        ef = efficiency_checks.get(key, []) if minutes == 1 else []
        slopes = slope_checks.get((key, minutes), [])
        if not ef and not slopes:
            continue
        times = [r["end"] for r in records]
        for time, direction, value in ef:
            i = bisect_right(times, time) - 1
            require(i >= 10 and times[i] == time, "效率窗口不足")
            prices = [r["close"] for r in records[i - 10 : i + 1]]
            distance = sum(
                abs(y - x) for x, y in zip(prices[:-1], prices[1:], strict=True)
            )
            actual = (
                (1 if direction == "LONG" else -1) * (prices[-1] - prices[0]) / distance
            )
            close(value, actual, "11个完成收盘价独立效率")
            checks["efficiency_closes"] += 1
        for time, direction, point in slopes:
            i = bisect_right(times, time) - 1
            h = point["lookback_bars"]
            require(i >= h + 19 and times[i] <= time, "斜率使用未来或预热不足")
            ma = sum(r["close"] for r in records[i - 19 : i + 1]) / 20
            previous = sum(r["close"] for r in records[i - h - 19 : i - h + 1]) / 20
            actual = (
                (1 if direction == "LONG" else -1)
                * (ma - previous)
                / (h * records[i]["previous_atr"])
            )
            close(point["signed_atr_per_bar"], actual, "独立20收盘均值斜率")
            checks["changed_slope_means"] += 1
    return {
        "status": "passed",
        "intrinsic_observations_checked": count,
        "state_changes": state_changes,
        "cost_observations_checked": cost_checked,
        "filter_changes": dict(changed),
        "independent_measurements": dict(checks),
        "reentry_blocked_observations": blocked,
        "future_prices_used": False,
    }


def audit(directory):
    run = Path(directory)
    cfg = json.loads((run / "config_snapshot.json").read_text())
    plan, _ = validate_optimization(cfg)
    parent = Path(plan["baselines"][cfg["optimization_review"]["month"]]["directory"])
    with patch("research.coverage_audit.audit_journal", audit_journal):
        result = audit_run(run)
    names = (
        [
            "trades",
            "signals",
            "orders",
            "events",
            "equity",
            "daily_pool",
            "pool_exclusions",
            "daily_candidates",
            "candidate_execution",
            "rank_contribution",
        ]
        if cfg["optimization_review"]["variant"] == "control"
        else [
            "daily_pool",
            "pool_exclusions",
            "daily_candidates",
            "candidate_execution",
        ]
    )
    exact = {
        name: file_sha256(run / (name + ".csv.gz"))
        == file_sha256(parent / (name + ".csv.gz"))
        for name in names
    }
    require(all(exact.values()), "对照或排名未精确复现：" + str(exact))
    result["exact_csv_matches"] = exact
    write_json(
        run / "independent_frequency_audit.json", result, SpaceBudget(plan["budget"])
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "directory": str(run),
                "net": result["net"],
                "trades": len(result["trades"]),
                "journal": result["optimization_checks"]["journal"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    audit(parser.parse_args().directory)
