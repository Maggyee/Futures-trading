"""Recompute trade arithmetic, slopes, protection and complete trailing paths."""

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import talib

from .config import ResearchError, digest
from .coverage_expansion import ORIGINAL, declaration
from .data import file_sha256
from .feature_cache import read_frames
from .optimization_audit import afternoon_requirements, audit_afternoon, audit_journal, audit_pullbacks, audit_source_archive
from .reporting import write_json
from .storage import SpaceBudget


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def helper(name):
    path = Path(__file__).resolve().parents[1] / "research_inputs/2026-09" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, {"path": str(path), "sha256": file_sha256(path)}


def audit_run(directory):
    run = Path(directory)
    cfg = json.loads((run / "config_snapshot.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    summary = json.loads((run / "summary.json").read_text())
    if manifest["window"]["end"] >= cfg["splits"]["test"]["start"]:
        raise ResearchError("审计拒绝读取锁定测试")
    trades = list(rows(run / "trades.csv.gz"))
    needed = {(t["contract"],t["entry_signal_time"]) for t in trades}
    signals = { (r["contract"],r["time"]):r for r in rows(run / "signals.csv.gz") if (r["contract"],r["time"]) in needed }
    if set(signals) != needed:
        raise ResearchError("成交入场信号不完整")
    source_reference = run / "data_reference.json"
    if manifest["window"]["start"] == "2026-09-14":
        source_reference = ORIGINAL / "data_reference.json"
    reference = json.loads(source_reference.read_text())
    source = (source_reference.parent / reference["object"]).resolve()
    if file_sha256(source) != reference["sha256"]:
        raise ResearchError("独立审计的原始行情指纹不符")
    traded_keys = {t["contract"] for t in trades}
    afternoon_cohorts, afternoon_needed = afternoon_requirements(run, cfg)
    raw = {}
    with gzip.open(source, "rt") as stream:
        next(stream)
        for line in stream:
            record = json.loads(line); bar = record["row"]
            if bar["trading_day"] > manifest["window"]["end"]:
                raise ResearchError("审计行情包含验证截止之后的数据")
            key = bar["symbol"] + "." + bar["exchange"]
            if record["kind"] == "bar" and (key in traded_keys or (key, bar["datetime"]) in afternoon_needed):
                raw[(key,bar["datetime"])] = bar
    if (run / "prepared_source_review.json").exists():
        evidence = json.loads((run / "prepared_source_review.json").read_text())
        cache, cache_key = Path(evidence["cache"]), evidence["cache_key"]
        if file_sha256(cache) != evidence["cache_sha256"]:
            raise ResearchError("原指标缓存指纹不符")
    else:
        cache_key = digest({"algorithm":"causal-sma-talib-wilder-complete-session-v1",
            "data":manifest["data_fingerprint"], "source":hashlib.sha256(Path(__file__).with_name("signals.py").read_bytes()).hexdigest(),
            "talib":talib.__version__, "atr":cfg["strategy"]["atr_period"], "night":cfg["strategy"]["include_night_indicators"],
            "calendar":cfg["calendar"], "metadata":cfg["metadata"]})
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (cache_key + ".jsonl.gz")
    frames,indices,slope_frames = {},{},{}
    for key,minutes,records in read_frames(cache,cache_key):
        if key not in traded_keys:
            continue
        if any(r["day"] > manifest["window"]["end"] for r in records):
            raise ResearchError("独立指标审计包含未来日期")
        if minutes == 1:
            frames[key] = records
            indices[key] = {r["end"]:i for i,r in enumerate(records)}
        if minutes in (1,5):
            slope_frames[(key,str(minutes)+"m")] = ([r["end"] for r in records],[(r["ma20"],r["previous_atr"]) for r in records])
            # Independently average the twenty completed closes used by each
            # traded signal, instead of trusting the stored MA20 numbers.
            for t in (t for t in trades if t["contract"] == key):
                from bisect import bisect_right
                index = bisect_right([r["end"] for r in records],t["entry_signal_time"]) - 1
                h = cfg["strategy"]["slope_band"]["timeframes"][str(minutes)+"m"]["lookback_bars"]
                for j in (index,index-h):
                    average = sum(r["close"] for r in records[j-19:j+1]) / 20
                    if j < 19 or not math.isclose(average,records[j]["ma20"],rel_tol=1e-9,abs_tol=1e-7):
                        raise ResearchError("独立MA20均值与入场斜率来源不符")
    trailing,trailing_source = helper("audit_trailing_exit")
    slope,slope_source = helper("audit_slope_band")
    trailing.load_market = lambda run,trades,cfg:(raw,frames,indices)
    metadata = {}
    for row in cfg["metadata"]["contracts"]:
        metadata.setdefault(row["symbol"]+"."+row["exchange"],[]).append(row)
    for signal in signals.values():
        slope.audit_slope(signal,slope_frames,cfg,metadata)
    arithmetic,paths,net,fees,risk_observations = trailing.audit_trades(run,cfg,signals)
    optimization_checks = {
        "journal": audit_journal(run, cfg, trailing.fee),
        "pullback": audit_pullbacks(cfg, trades, signals, frames, indices),
        "afternoon": audit_afternoon(run, cfg, afternoon_cohorts, raw, trades),
    }
    if cfg.get("optimization_review"):
        optimization_checks["source_archive"] = audit_source_archive(run)
    checked_sources, supplier_assumptions = set(), set()
    for row in cfg["execution"]["qualification"]["rules"]:
        for source_record in row["sources"]:
            if "path" not in source_record:
                if source_record.get("historical_applicability_verified") is not False or source_record.get("url") != "https://openmd.shinnytech.com/t/md/symbols/latest.json":
                    raise ResearchError("执行资料缺少可核对文件或明确供应商延续假设")
                supplier_assumptions.add(source_record["url"])
                continue
            if source_record["path"] in checked_sources:
                continue
            if file_sha256(source_record["path"]) != source_record["sha256"]:
                raise ResearchError("执行资料的原始下载指纹不符")
            checked_sources.add(source_record["path"])
    result = {"status":"passed", "directory":str(run), "trades":arithmetic,
              "paths":paths, "net":net,"fees":fees,"risk_observations":risk_observations,
              "independent_slope_arithmetic":True,"independent_twenty_close_means":True,
              "independent_exit_matching":True,"source_data_sha256":reference["sha256"],
              "physical_execution_sources_verified":len(checked_sources),"supplier_specification_assumptions":sorted(supplier_assumptions),
              "cache_sha256":file_sha256(cache),"audit_helpers":[trailing_source,slope_source],"locked_test_read":False}
    if cfg.get("optimization_review"):
        result["optimization_checks"] = optimization_checks
        result["optimization_audit_sha256"] = file_sha256(Path(__file__).with_name("optimization_audit.py"))
    write_json(run / "independent_coverage_audit.json",result,SpaceBudget(cfg["storage"]["budget"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan",default=str(Path(__file__).resolve().parents[1]/"research_inputs/coverage_expansion_2026-10-05/plan.json"))
    parser.add_argument("--directory")
    args = parser.parse_args()
    path,plan,parent = declaration(args.plan)
    root = Path(plan["budget"]["roots"][1])
    targets = [Path(args.directory)] if args.directory else [Path(json.loads(p.read_text())["directory"]) for p in sorted(root.glob("*_k*_latest.json"))]
    reviews = []
    for target in targets:
        result = audit_run(target)
        reviews.append({k:result[k] for k in ("directory","status","net","fees","risk_observations")})
        print(json.dumps(reviews[-1],ensure_ascii=False),flush=True)
    if not args.directory:
        control = root / "2026-09_k2_latest.json"
        if control.exists():
            run = Path(json.loads(control.read_text())["directory"])
            if list(rows(run/"trades.csv.gz")) != list(rows(Path(plan["parent_run"])/"trades.csv.gz")):
                raise ResearchError("当前K=2控制组未能精确复现原成交")
        write_json(root / "independent_review.json",{"status":"passed","runs":reviews,"locked_test_read":False},SpaceBudget(plan["budget"]))


if __name__ == "__main__":
    main()
