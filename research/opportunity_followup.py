"""Run the frozen opportunity recipe in order, without retuning from its PnL."""

import argparse
import copy
import csv
import gzip
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from .config import read_config, validate_config
from .coverage_expansion import ORIGINAL, ROOT
from .data import file_sha256, load_data
from .experiments import run_one
from .frequency_followup import require
from .opportunity_rules import OpportunityBacktest, TrendWindowLogic
from .optimization_declaration import validate_optimization
from .prepared_review import prepare_review
from .reporting import write_json
from .signals import Features
from .storage import SpaceBudget

PLAN = ROOT / "research_inputs/coverage_expansion_2026-10-05/opportunity_followup_plan.json"


class BaselineEntries:
    """Reuse unchanged measurements by exact clock/key; new rank slots evaluate fresh."""

    def __init__(self, parent, cfg):
        self.parent, self.cfg = Path(parent), cfg
        self.path = self.parent / "signals.csv.gz"
        with tarfile.open(self.parent / "source_snapshot.tar.gz") as archive:
            for name in ("signals.py", "refinements.py"):
                require(archive.extractfile("research/" + name).read() == Path(__file__).with_name(name).read_bytes(),
                        "原指标/基础入场算法已改变：" + name)
        self.checked, self.reused, self.fresh, self.skipped = set(), 0, 0, 0
        self.filter_order = None
        self.evidence = {"source_run": str(self.parent),
                         "source_signals_sha256": file_sha256(self.path),
                         "source_fills_or_profit_used": False, "future_measurements_used": False,
                         "only_exact_clock_contract_measurements_reused": True,
                         "portfolio_allocation_and_matching_recomputed": True,
                         "locked_test_read": False}

    def bind(self, original):
        self.original = original
        self.data, self.features = original.data, original.features
        self.trend = TrendWindowLogic(original, self.baseline) if self.cfg["strategy"].get("trend_entry") else None
        return self

    def __enter__(self):
        self.stream = gzip.open(self.path, "rt", encoding="utf-8-sig")
        self.reader = csv.DictReader(self.stream)
        self.peek = next(self.reader, None)
        return self

    def __exit__(self, *unused):
        self.stream.close()

    def baseline(self, bar, candidate, state_allows=True, before_cutoff=True):
        target = bar.end.isoformat(), bar.key
        while self.peek is not None and (self.peek["time"], self.peek["contract"]) < target:
            self.skipped += 1
            self.peek = next(self.reader, None)
        if self.peek is None or (self.peek["time"], self.peek["contract"]) != target:
            self.fresh += 1
            return self.original.evaluate(bar, candidate, state_allows, before_cutoff)
        row = self.peek
        self.peek = next(self.reader, None)
        require(row["direction"] == candidate["direction"] and int(row["rank"]) == candidate["rank"],
                "原方向/排名改变")
        filters, snapshot = json.loads(row["filters"]), json.loads(row["snapshot"])
        filters.pop("cost", None)
        filters.pop("stop_reentry", None)
        require(filters["entry_time"] == before_cutoff, "原入场时间界改变")
        fresh = None
        if bar.key not in self.checked:
            fresh = self.original.evaluate(bar, candidate, state_allows, before_cutoff)
            if self.filter_order is None:
                self.filter_order = tuple(fresh["filters"])
            require(tuple(fresh["filters"]) == self.filter_order, "基础过滤顺序改变")
        require(self.filter_order is not None and set(filters) == set(self.filter_order),
                "缓存过滤字段不符")
        # CSV stores nested dictionaries in sorted order; recover the source's
        # evaluation order before the engine recomputes the rejection list.
        filters = {name: filters[name] for name in self.filter_order}
        filters["candidate"], filters["state"] = bool(candidate["selected"]), state_allows
        result = {"filters": filters, "rejections": [k for k, v in filters.items() if not v],
                  "all_pass": all(filters.values()),
                  "pullback": json.loads(row["pullback"]) if row["pullback"] else None,
                  "snapshot": snapshot, "exit_flags": json.loads(row["exit_flags"])}
        if fresh is not None:
            require(all(fresh[k] == result[k] for k in ("filters", "snapshot", "exit_flags")),
                    "按合约独立实时测量未匹配")
            self.checked.add(bar.key)
        self.reused += 1
        return result

    def evaluate(self, *args, **kwargs):
        return (self.trend.evaluate if self.trend else self.baseline)(*args, **kwargs)

    def finish(self):
        if self.peek is not None:
            self.skipped += 1 + sum(1 for _ in self.reader)
        if not self.cfg["strategy"].get("candidate_replacement"):
            require(self.skipped == self.fresh == 0, "固定候选复用有遗漏或新观察")
        self.evidence.update(observations_replayed=self.reused, fresh_observations=self.fresh,
                             unselected_original_observations=self.skipped,
                             first_measurement_checked_contracts=sorted(self.checked))


def configuration(month, variant, plan_path=PLAN):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    require(variant in plan["variants"], "未冻结的实验版本")
    parent = Path(plan["baselines"][month]["directory"])
    cfg = read_config(parent / "config_snapshot.json")
    cfg["strategy"].update(copy.deepcopy(plan["variants"][variant]))
    cfg["optimization_review"] = {
        "plan": str(plan_path), "plan_sha256": file_sha256(plan_path),
        "month": month, "variant": variant,
        "sample_status": plan["sample_status"], "locked_test_read": False,
    }
    cfg["baseline_expectation"]["strategy"]["entry_mode"] = cfg["strategy"]["entry_mode"]
    cfg["storage"]["compress_signal_journal"] = True
    cfg["storage"]["budget"] = copy.deepcopy(plan["budget"])
    validate_config(cfg)
    validate_optimization(cfg)
    return plan, parent, cfg


def run(month, variant, *, plan_path=PLAN, engine_factory=OpportunityBacktest,
        entries_factory=BaselineEntries):
    plan, parent, cfg = configuration(month, variant, plan_path)
    output = Path(plan["output"])
    target = output / (month + "_" + variant + "_latest.json")
    if target.exists():
        record = json.loads(target.read_text())
        require(read_config(Path(record["directory"]) / "config_snapshot.json") == cfg
                and record["status"] == "completed", "已有实验配置/状态不符")
        return record
    budget = SpaceBudget(plan["budget"])
    budget.check(output, reserve=75 * 1024 * 1024)
    require(len(list(output.glob("*/*/run_*/manifest.json"))) < plan["maximum_runs"],
            "达到冻结实验数量上限")
    if variant != "control":
        pointer = output / (month + "_control_latest.json")
        require(pointer.exists(), "先完成本窗口对照")
        control = json.loads(pointer.read_text())
        proof = Path(control["directory"]) / plan.get("audit_filename", "independent_opportunity_audit.json")
        require(proof.exists() and json.loads(proof.read_text())["status"] == "passed",
                "对照尚未通过审计")
    window = json.loads((parent / "manifest.json").read_text())["window"]
    if month == "2026-09":
        data, features, evidence = prepare_review(ORIGINAL, cfg)
    else:
        data = load_data(cfg, cutoff=window["end"])
        expected = json.loads((parent / "manifest.json").read_text())["data_fingerprint"]
        require(data.fingerprint == expected, "历史行情指纹不符")
        features = Features(data, cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (features.cache_key + ".jsonl.gz")
        evidence = {"source_run": str(parent), "retained_data_fingerprint": data.fingerprint,
                    "cache": str(cache), "cache_key": features.cache_key,
                    "cache_sha256": file_sha256(cache),
                    "feature_algorithm_and_accessors_unchanged": True, "locked_test_read": False}
    print(json.dumps({"phase": "prepared", "month": month, "variant": variant,
                      "bars": len(data.bars)}, ensure_ascii=False), flush=True)
    entries = entries_factory(parent, cfg)
    directory, result = run_one(
        data, cfg, output / variant / month, window,
        split="retrospective" if month != "2026-09" else "validation",
        prepared_features=features, prepared_entries=entries,
        engine_factory=engine_factory if variant != "control" else None,
    )
    write_json(directory / "prepared_source_review.json", evidence, budget)
    write_json(directory / "prepared_entries_review.json", entries.evidence, budget)
    require(result["status"] == "completed", "回放失败：" + str(result.get("error")))
    record = {"month": month, "variant": variant, "directory": str(directory),
              "parent": str(parent), "window": window, "status": "completed",
              "metrics": result["metrics"], "locked_test_read": False}
    write_json(target, record, budget)
    for name in ("signals", *getattr(engine_factory, "extra_journals", ())):
        if hasattr(result.get(name), "discard"):
            result[name].discard()
    print(json.dumps({"phase": "completed", "month": month, "variant": variant,
                      "trades": result["metrics"]["trade_count"],
                      "net": result["metrics"]["net_profit"]}, ensure_ascii=False), flush=True)
    return record


def run_stage(variant):
    plan = json.loads(PLAN.read_text())
    for month in sorted(plan["baselines"]):
        subprocess.run([sys.executable, "-m", "research.opportunity_followup", "run", "--month", month,
                        "--variant", variant], check=True)
        record = json.loads((Path(plan["output"]) / (month + "_" + variant + "_latest.json")).read_text())
        subprocess.run([sys.executable, "-m", "research.opportunity_audit", "--directory",
                        record["directory"]], check=True)


def all_stages():
    from .opportunity_assessment import assess

    plan = json.loads(PLAN.read_text())
    for variant in plan["order"]:
        run_stage(variant)
        assess(final=False)
    a = assess(final=False)
    entry = next((v for v in ("replacement", "trend") if a["promotion"][v]["passed"]), None)
    if all(a["promotion"][v]["passed"] for v in ("replacement", "trend")):
        run_stage("replacement_trend")
        a = assess(final=False)
        if a["promotion"]["replacement_trend"]["passed"]:
            entry = "replacement_trend"
    exit_name = next((v for v in ("breakeven15", "ma40confirm") if a["promotion"][v]["passed"]), None)
    if entry and exit_name:
        run_stage(entry + "_" + exit_name)
    assess(final=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "all", "stage"))
    parser.add_argument("--month")
    parser.add_argument("--variant")
    args = parser.parse_args()
    if args.action == "all":
        all_stages()
    elif args.action == "stage":
        run_stage(args.variant)
    else:
        run(args.month, args.variant)
