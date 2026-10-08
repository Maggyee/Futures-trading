"""Predeclared entry-frequency experiments on already inspected windows."""

import argparse
import copy
import csv
import gzip
import hashlib
import json
import math
import subprocess
import sys
import tarfile
from pathlib import Path

from .config import ResearchError, read_config
from .coverage_expansion import ORIGINAL, ROOT
from .data import file_sha256, load_data
from .experiments import run_one
from .optimization_declaration import validate_optimization
from .prepared_review import prepare_review
from .reporting import write_json
from .signals import Features
from .storage import SpaceBudget

PLAN = (
    ROOT / "research_inputs/coverage_expansion_2026-10-05/frequency_followup_plan.json"
)


def require(value, message):
    if not value:
        raise ResearchError(message)


def recompute_saved(row, strategy):
    """Reuse measured values, re-evaluate declared market gates, never fills/PnL."""
    filters = json.loads(row["filters"])
    snapshot = json.loads(row["snapshot"])
    # These gates depend on live execution state and are recomputed by the engine.
    filters.pop("cost", None)
    filters.pop("stop_reentry", None)
    efficiency = snapshot.get("efficiency")
    filters["efficiency"] = bool(
        efficiency is not None
        and math.isfinite(efficiency)
        and efficiency >= strategy["efficiency_min"]
    )
    delta = snapshot.get("oi_delta")
    filters["oi"] = (
        bool(delta is not None and delta > 0) if strategy["enable_oi_filter"] else True
    )
    for label in ("1m", "5m"):
        rule = strategy["slope_band"]["timeframes"][label]
        point = snapshot["slope_band"][label]
        require(
            point["lookback_bars"] == rule["lookback_bars"], "不能改变快照斜率回看根数"
        )
        point.update({k: rule[k] for k in ("min_atr_per_bar", "max_atr_per_bar")})
        require(
            point["min_move_ticks"] == strategy["slope_band"]["min_move_ticks"],
            "不能改变斜率跳数下限",
        )
        normalized, ticks = (
            point.get("signed_atr_per_bar"),
            point.get("signed_move_ticks"),
        )
        ready = filters["slope_" + label + "_ready"]
        filters["slope_" + label + "_minimum"] = bool(
            ready
            and normalized >= rule["min_atr_per_bar"] - 1e-12
            and ticks >= point["min_move_ticks"] - 1e-8
        )
        filters["slope_" + label + "_maximum"] = bool(
            ready and normalized <= rule["max_atr_per_bar"] + 1e-12
        )
    return filters, snapshot


class FollowupEntries:
    """Strict same-window reuse; declared filters and all portfolio state rerun."""

    def __init__(self, parent, cfg, evidence):
        plan, old = validate_optimization(cfg)
        self.parent, self.cfg = Path(parent), cfg
        record = plan["baselines"][cfg["optimization_review"]["month"]]
        require(self.parent == Path(record["directory"]), "复用来源并非本轮基准")
        require(
            old["storage"].get("record_unselected_signals") is False
            and cfg["storage"].get("record_unselected_signals") is False,
            "本轮保持原选中候选日志范围",
        )
        manifest = json.loads((self.parent / "manifest.json").read_text())
        require(
            manifest["data_fingerprint"] == evidence["retained_data_fingerprint"],
            "复用行情指纹不符",
        )
        expected = cfg.get("archive_evaluation", {}).get(
            "window", cfg["splits"]["validation"]
        )
        require(
            manifest["window"] == expected
            and manifest["scope"] == "shared"
            and expected["end"] < cfg["splits"]["test"]["start"],
            "复用时间/资金范围不符",
        )
        require(
            manifest["split"]
            == ("retrospective" if cfg.get("archive_evaluation") else "validation"),
            "复用样本身份不符",
        )
        for key in (
            "risk",
            "metadata",
            "calendar",
            "splits",
            "execution",
            "calibration_snapshot",
            "data",
        ):
            require(cfg[key] == old[key], "禁止改变复用资料：" + key)
        allowed = {
            "efficiency_min",
            "enable_oi_filter",
            "slope_band",
            "block_same_day_reentry_after_stop",
        }
        require(
            {k: v for k, v in cfg["strategy"].items() if k not in allowed}
            == {k: v for k, v in old["strategy"].items() if k not in allowed},
            "复用改变了未允许的规则",
        )
        sources = {}
        with tarfile.open(self.parent / "source_snapshot.tar.gz") as archive:
            for name in ("signals.py", "refinements.py"):
                current = Path(__file__).with_name(name).read_bytes()
                require(
                    archive.extractfile("research/" + name).read() == current,
                    "指标/原信号算法已改变",
                )
                sources[name] = hashlib.sha256(current).hexdigest()
        self.path = self.parent / "signals.csv.gz"
        self.evidence = {
            "source_run": str(self.parent),
            "source_signals_sha256": file_sha256(self.path),
            "unchanged_entry_sources": sources,
            "data_fingerprint": manifest["data_fingerprint"],
            "only_declared_filters_recomputed": True,
            "portfolio_state_execution_cost_and_cash_recomputed": True,
            "source_fills_or_profit_used": False,
            "locked_test_read": False,
        }
        self.count, self.first_checked = 0, False

    def bind(self, original_logic):
        self.original_logic = original_logic
        return self

    def __enter__(self):
        self.stream = gzip.open(self.path, "rt", encoding="utf-8-sig")
        self.reader = csv.DictReader(self.stream)
        return self

    def __exit__(self, *unused):
        self.stream.close()

    def evaluate(self, bar, candidate, state_allows=True, before_cutoff=True):
        row = next(self.reader, None)
        require(row is not None, "快照提前结束")
        identity = {
            "time": bar.end.isoformat(),
            "date": bar.trading_day,
            "contract": bar.key,
            "direction": candidate["direction"],
            "rank": str(candidate["rank"]),
        }
        require(all(row[k] == v for k, v in identity.items()), "候选身份/顺序改变")
        filters, snapshot = recompute_saved(row, self.cfg["strategy"])
        require(
            filters["candidate"] is True and filters["entry_time"] == before_cutoff,
            "候选或时间门槛改变",
        )
        filters["state"] = state_allows
        flags = json.loads(row["exit_flags"])
        pullback = json.loads(row["pullback"]) if row["pullback"] else None
        if not self.first_checked:
            fresh = self.original_logic.evaluate(
                bar, candidate, state_allows, before_cutoff
            )
            require(
                fresh["filters"] == filters
                and fresh["snapshot"] == snapshot
                and fresh["exit_flags"] == flags
                and fresh["pullback"] == pullback,
                "第一行实时重算未匹配",
            )
            self.order = list(fresh["filters"])
            self.first_checked = True
        filters = {key: filters[key] for key in self.order}
        self.count += 1
        return {
            "filters": filters,
            "snapshot": snapshot,
            "rejections": [k for k, v in filters.items() if not v],
            "all_pass": all(filters.values()),
            "exit_flags": flags,
            "pullback": pullback,
        }

    def finish(self):
        require(next(self.reader, None) is None, "未回放全部分钟观察")
        self.evidence["observations_replayed"] = self.count


def configuration(plan_path, month, variant):
    path = Path(plan_path).resolve()
    plan = json.loads(path.read_text())
    require(variant in plan["variants"], "未声明的实验")
    parent = Path(plan["baselines"][month]["directory"])
    cfg = read_config(parent / "config_snapshot.json")
    cfg["strategy"].update(copy.deepcopy(plan["variants"][variant]))
    cfg["optimization_review"] = {
        "plan": str(path),
        "plan_sha256": file_sha256(path),
        "month": month,
        "variant": variant,
        "sample_status": plan["sample_status"],
        "locked_test_read": False,
    }
    validate_optimization(cfg)
    return plan, parent, cfg


def run(plan_path, month, variant):
    plan, parent, cfg = configuration(plan_path, month, variant)
    output = Path(plan["output"])
    latest = output / (month + "_" + variant + "_latest.json")
    if latest.exists():
        record = json.loads(latest.read_text())
        require(
            read_config(Path(record["directory"]) / "config_snapshot.json") == cfg
            and record["status"] == "completed",
            "已存在不同配置结果",
        )
        return record
    if variant != "control":
        control = json.loads((output / (month + "_control_latest.json")).read_text())
        require(
            json.loads(
                (
                    Path(control["directory"]) / "independent_frequency_audit.json"
                ).read_text()
            )["status"]
            == "passed",
            "先完成对照核验",
        )
    window = json.loads((parent / "manifest.json").read_text())["window"]
    if month == "2026-09":
        data, features, evidence = prepare_review(ORIGINAL, cfg)
    else:
        data = load_data(cfg, cutoff=window["end"])
        expected = json.loads((parent / "manifest.json").read_text())[
            "data_fingerprint"
        ]
        require(data.fingerprint == expected, "历史行情与对照不同")
        features = Features(data, cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"]) / (
            features.cache_key + ".jsonl.gz"
        )
        evidence = {
            "source_run": str(parent),
            "retained_data_fingerprint": data.fingerprint,
            "cache": str(cache),
            "cache_key": features.cache_key,
            "cache_sha256": file_sha256(cache),
            "feature_algorithm_and_accessors_unchanged": True,
            "locked_test_read": False,
        }
    print(
        json.dumps(
            {
                "phase": "prepared",
                "month": month,
                "variant": variant,
                "bars": len(data.bars),
            }
        ),
        flush=True,
    )
    entries = FollowupEntries(parent, cfg, evidence)
    directory, result = run_one(
        data,
        cfg,
        output / variant / month,
        window,
        split="retrospective" if month != "2026-09" else "validation",
        prepared_features=features,
        prepared_entries=entries,
    )
    budget = SpaceBudget(plan["budget"])
    write_json(directory / "prepared_source_review.json", evidence, budget)
    write_json(directory / "prepared_entries_review.json", entries.evidence, budget)
    require(result["status"] == "completed", "回测失败：" + str(result.get("error")))
    record = {
        "month": month,
        "variant": variant,
        "directory": str(directory),
        "parent": str(parent),
        "window": window,
        "status": "completed",
        "metrics": result["metrics"],
        "locked_test_read": False,
    }
    write_json(latest, record, budget)
    if hasattr(result["signals"], "discard"):
        result["signals"].discard()
    print(
        json.dumps(
            {
                "phase": "completed",
                "month": month,
                "variant": variant,
                "trades": result["metrics"]["trade_count"],
                "net": result["metrics"]["net_profit"],
            }
        ),
        flush=True,
    )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(PLAN))
    parser.add_argument("--month")
    parser.add_argument("--variant")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all:
        plan = json.loads(Path(args.plan).read_text())
        output = Path(plan["output"])
        output.mkdir(parents=True, exist_ok=True)
        for variant in plan["order"]:
            for month in ["2026-09", "2026-07", "2026-08"]:
                print(
                    json.dumps(
                        {"phase": "starting", "month": month, "variant": variant}
                    ),
                    flush=True,
                )
                with (output / (variant + "_" + month + ".log")).open("a") as log:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "research.frequency_followup",
                            "--plan",
                            args.plan,
                            "--month",
                            month,
                            "--variant",
                            variant,
                        ],
                        stdout=log,
                        stderr=log,
                        check=True,
                    )
                record = json.loads(
                    (output / (month + "_" + variant + "_latest.json")).read_text()
                )
                with (output / (variant + "_" + month + "_audit.log")).open("a") as log:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "research.frequency_audit",
                            "--directory",
                            record["directory"],
                        ],
                        stdout=log,
                        stderr=log,
                        check=True,
                    )
                print(
                    json.dumps(
                        {
                            "phase": "audited",
                            "month": month,
                            "variant": variant,
                            "trades": record["metrics"]["trade_count"],
                            "net": record["metrics"]["net_profit"],
                        }
                    ),
                    flush=True,
                )
    elif args.month and args.variant:
        run(args.plan, args.month, args.variant)
    else:
        parser.error("需要--all或--month/--variant")


if __name__ == "__main__":
    main()
