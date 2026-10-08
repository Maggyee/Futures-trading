"""Independent checks of the predeclared entry and candidate experiments.

No production entry, cost or ranking helper is called. Large minute journals
are compared as streams; only the eight-minute afternoon cohorts are retained.
"""

import csv
import gzip
import hashlib
import itertools
import json
import math
import tarfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .calendar import Calendar
from .config import ResearchError, digest
from .data import file_sha256
from .optimization_declaration import validate_optimization


def require(condition, message):
    if not condition:
        raise ResearchError(message)


def close(actual, expected, name):
    require(math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-7),
            "独立优化核验不符：" + name)


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def audit_source_archive(run):
    run = Path(run)
    manifest = json.loads((run/"manifest.json").read_text())
    expected = manifest["source_hashes"]
    require(digest(expected) == manifest["code_hash"], "回放来源清单与代码总指纹不符")
    path = run/"source_snapshot.tar.gz"
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) == len(expected) and {r.name for r in members} == set(expected), "回放代码快照成员遗漏或重复")
        for member in members:
            require(member.isfile(), "回放代码快照包含非文件成员")
            actual = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            require(actual == expected[member.name], "回放代码快照与来源指纹不符：" + member.name)
    return {"status": "passed", "files_checked": len(expected), "code_hash": manifest["code_hash"], "archive_sha256": file_sha256(path)}


def afternoon_requirements(run, cfg):
    if not cfg["strategy"].get("afternoon_rerank"):
        return {}, set()
    cal = Calendar(cfg["calendar"])
    cohorts, needed = {}, set()
    for row in rows(Path(run) / "daily_pool.csv.gz"):
        day, key = row["date"], row["contract"]
        applicable = [m for m in cfg["metadata"]["contracts"]
                      if m["symbol"] + "." + m["exchange"] == key
                      and m.get("effective_from", "") <= day
                      and (not m.get("effective_to") or day <= m["effective_to"])]
        require(bool(applicable), "午后审计缺少当日合约资料")
        meta = max(applicable, key=lambda m: m.get("effective_from", ""))
        openings = [a for a, _ in cal.periods(day, meta) if a.hour >= 12]
        if not openings:
            continue
        opening = openings[0]
        identity = (day, opening.isoformat())
        cohorts.setdefault(identity, []).append(row)
        needed.update((key, (opening + timedelta(minutes=i)).isoformat()) for i in range(8))
    return cohorts, needed


def audit_afternoon(run, cfg, cohorts, raw, trades):
    if not cfg["strategy"].get("afternoon_rerank"):
        return None
    from datetime import datetime

    expected, unavailable, flat = {}, [], []
    for (day, clock), pool in cohorts.items():
        opening = datetime.fromisoformat(clock)
        ranking_time = (opening + timedelta(minutes=8)).isoformat()
        ready = []
        for member in pool:
            key = member["contract"]
            bars = [raw.get((key, (opening + timedelta(minutes=i)).isoformat())) for i in range(8)]
            if any(b is None for b in bars) or any(b["open_interest"] is None or not b["tradable"] for b in bars) or sum(b["volume"] for b in bars) <= 0:
                unavailable.append({"date": day, "contract": key, "opening": clock})
                continue
            change = bars[-1]["close"] / bars[0]["open"] - 1
            if change == 0:
                flat.append({"date": day, "contract": key, "opening": clock})
                continue
            ready.append(member | {"r8": change, "direction": "LONG" if change > 0 else "SHORT",
                                   "opening": clock, "ranking_time": ranking_time})
        partitions = {(r["group"], r["direction"]) for r in ready}
        for group, direction in partitions:
            ordered = sorted((r for r in ready if (r["group"], r["direction"]) == (group, direction)),
                             key=lambda r: (-abs(r["r8"]), -float(r["previous_volume"]), r["contract"]))
            for rank, row in enumerate(ordered, 1):
                expected[(day, row["contract"])] = row | {"rank": rank, "selected": rank <= cfg["strategy"]["k"]}
    actual = {}
    for row in rows(Path(run) / "daily_candidates.csv.gz"):
        if row.get("ranking_phase") != "afternoon":
            continue
        identity = (row["date"], row["contract"])
        require(identity not in actual, "同一合约重复午后排名")
        actual[identity] = row
    require(set(actual) == set(expected), "午后排名遗漏或加入了观察期不可用的合约")
    for identity, row in actual.items():
        original = expected[identity]
        for field in ("contract", "date", "group", "direction", "opening", "ranking_time", "selection_day"):
            require(row[field] == original[field], "午后候选资料不符：" + field)
        for field in ("r8", "previous_volume", "previous_oi", "rank"):
            close(row[field], original[field], field)
        require(row["selected"] == str(original["selected"]) and int(row["k"]) == cfg["strategy"]["k"], "午后选取超出原K")
        require(row["opening_price_definition"] == "afternoon_first_minute_open", "午后开盘定义不符")
    entries = 0
    for trade in trades:
        day, key, signal_time = trade["entry_signal_time"][:10], trade["contract"], trade["entry_signal_time"]
        phase = next((clock for d, clock in cohorts if d == day and any(r["contract"] == key for r in cohorts[(d, clock)])), None)
        if phase is None or signal_time < (datetime.fromisoformat(phase) + timedelta(minutes=8)).isoformat():
            continue
        candidate = expected.get((day, key))
        require(candidate is not None and candidate["selected"], "午后刷新后仍使用过期或未选候选入场")
        require(trade["direction"] == candidate["direction"] and int(trade["rank"]) == candidate["rank"], "午后成交方向或排名不符")
        close(trade["r8"], candidate["r8"], "成交午后涨跌幅")
        entries += 1
    return {"status": "passed", "cohorts_checked": len(cohorts), "candidates_checked": len(actual),
            "afternoon_entries_checked": entries, "unavailable": unavailable, "flat": flat,
            "observations_per_candidate": 8, "future_bars_used": False}


def audit_pullbacks(cfg, trades, signals, frames, indices):
    if cfg["strategy"]["entry_mode"] != "pullback_ma10":
        return None
    checked, consumed = [], set()
    for trade in trades:
        key, end = trade["contract"], trade["entry_signal_time"]
        sign = 1 if trade["direction"] == "LONG" else -1
        data, index = frames[key], indices[key][end]
        require(index >= 23, "回踩均线历史不足")
        current, previous = data[index], data[index-1]
        require(sign * (current["close"] - previous["high" if sign > 0 else "low"]) > 0, "回踩未收盘突破前根K线")
        meta = max((m for m in cfg["metadata"]["contracts"] if m["symbol"] + "." + m["exchange"] == key and m.get("effective_from", "") <= end[:10] and (not m.get("effective_to") or end[:10] <= m["effective_to"])), key=lambda m: m.get("effective_from", ""))
        expected = None
        for j in range(index-1, index-4, -1):
            row, before = data[j], data[j-1]
            atr = row["previous_atr"]
            if atr is None or not math.isfinite(atr) or atr <= 0:
                continue
            for position in (j, j-1):
                for period in (10, 20):
                    mean = sum(r["close"] for r in data[position-period+1:position+1]) / period
                    close(data[position]["ma"+str(period)], mean, "回踩MA"+str(period))
            if not all(sign * (before["close"] - before["ma"+str(p)]) > 0 for p in (10, 20)):
                continue
            epsilon = max(cfg["strategy"]["pullback_epsilon_ticks"] * meta["tick_size"], cfg["strategy"]["pullback_epsilon_atr"] * atr)
            touched = [p for p in (10,20) if abs(row["low" if sign > 0 else "high"] - row["ma"+str(p)]) <= epsilon]
            if 10 in touched:
                expected = {"event": row["end"], "references": touched, "dual_touch": len(touched) == 2, "epsilon": float(epsilon)}
                break
        require(expected is not None, "成交之前不存在已完成的MA10回踩")
        actual = json.loads(trade["pullback"])
        require(actual == expected and actual == json.loads(signals[(key,end)]["pullback"]), "成交回踩事件不符")
        identity = (end[:10], key, actual["event"])
        require(identity not in consumed and actual["event"] < end, "回踩重复消费或引用未来K线")
        consumed.add(identity)
        checked.append({"contract": key, "signal_time": end, **actual})
    return {"status": "passed", "entries_checked": len(checked), "events": checked,
            "independent_close_means": True, "future_bars_used": False}


def audit_journal(run, cfg, fee_solver):
    if not cfg.get("optimization_review"):
        return None
    plan, old = validate_optimization(cfg)
    month, variant = (cfg["optimization_review"][field] for field in ("month", "variant"))
    base = Path(plan["baselines"][month]["directory"])
    if variant not in {"control", "cost", "breakeven", "pullback", "combined"}:
        return {"status": "passed", "declared_changes_verified": True}
    projection = old.get("storage", {}).get("record_unselected_signals", True) and not cfg.get("storage", {}).get("record_unselected_signals", True)
    skipped, count, state_changes, cost_checked, cost_rejected = 0, 0, 0, 0, 0
    def source_rows():
        nonlocal skipped
        for row in rows(base / "signals.csv.gz"):
            if projection and int(row["rank"]) > old["strategy"]["k"]:
                require(json.loads(row["filters"])["candidate"] is False, "独立投影发现原候选标记异常")
                skipped += 1
                continue
            yield row
    fields = ("time", "date", "contract", "direction", "rank", "r8", "group", "product", "snapshot", "pullback", "exit_flags", "execution_pass", "execution_rejections")
    if variant == "pullback":
        fields = tuple(field for field in fields if field != "pullback")
    rules = {(r["trading_day"], r["contract"]): r for r in cfg["execution"]["qualification"]["rules"]}
    for before, after in itertools.zip_longest(source_rows(), rows(Path(run) / "signals.csv.gz")):
        require(before is not None and after is not None, "优化分钟观察遗漏或多出")
        require(all(before[field] == after[field] for field in fields), "优化改变了原固有入场快照")
        left, right = (json.loads(row["filters"]) for row in (before, after))
        state_changes += left.pop("state") != right.pop("state")
        cost_pass = right.pop("cost", None)
        require(left == right, "优化改变了未声明的入场门槛")
        if cfg["strategy"].get("entry_cost_filter"):
            cost_checked += 1
            data = json.loads(after["cost_check"]) if after.get("cost_check") else None
            expected = False
            if after["execution_pass"] == "True":
                rule = rules[(after["date"], after["contract"])]
                require(data is not None, "可执行候选缺少成本诊断")
                snapshot = json.loads(after["snapshot"])
                atr = snapshot["atr_previous"]
                price = Decimal(str(data["price"]))
                costs = float(fee_solver(rule,1,"open",price) + fee_solver(rule,1,"close_today",price))
                distance = costs / rule["value_per_price"] + 2 * cfg["strategy"]["slippage_ticks"] * rule["tick_size"]
                close(data["roundtrip_fees_per_lot"], costs, "成本费用")
                close(data["roundtrip_price_distance"], distance, "成本价格距离")
                if atr is not None and math.isfinite(atr) and atr > 0:
                    close(data["signal_atr"], atr, "成本信号ATR")
                    close(data["cost_atr"], distance / atr, "成本ATR比率")
                    expected = distance / atr <= cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-12
                    if snapshot["extension"] is not None:
                        sign = 1 if after["direction"] == "LONG" else -1
                        close(data["price"], snapshot["ma20"] + sign * snapshot["extension"] * atr, "成本信号收盘价")
                else:
                    require(data["cost_atr"] is None, "缺失ATR仍计算成本门槛")
                require(data["accepted"] == expected, "成本诊断接受标记不符")
            require(cost_pass == expected, "成本过滤与独立计算不符")
            cost_rejected += not expected
        count += 1
    if variant == "control":
        for name in ("trades", "equity", "daily_pool", "daily_candidates", "candidate_execution"):
            require(all(a == b for a,b in itertools.zip_longest(rows(base/(name+".csv.gz")), rows(Path(run)/(name+".csv.gz")))), "控制组未精确复现：" + name)
    return {"status": "passed", "intrinsic_observations_checked": count, "state_changes": state_changes,
            "unselected_source_rows_projected_out": skipped, "cost_observations_checked": cost_checked,
            "cost_rejected_observations": cost_rejected, "declared_changes_verified": True,
            "source_journal_sha256": file_sha256(base/"signals.csv.gz")}
