"""Freeze a finite follow-up recipe before inspecting any new replay PnL."""

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from research.data import file_sha256
from research.feature_cache import read_frames

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
OUT = ROOT / "research_outputs/coverage_expansion_2026-10-05/frequency_followup"
TARGET = HERE / "frequency_followup_plan.json"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    if TARGET.exists():
        raise RuntimeError("声明已存在，不覆盖")
    OUT.mkdir(exist_ok=True)
    intent = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "order": ["control", "efficiency", "slope5", "oi", "stop_reentry"],
        "efficiency_min": 0.35,
        "slope5_quantiles": [0.1, 0.9],
        "slope5_training": "same original September 1-11 population; linear quantiles; keep 1m and one-tick floors unchanged",
        "oi": "disable only oi gate; preserve original observed OI",
        "stop_reentry": "after actual losing fixed-stop fill, block same contract and direction for remaining trading day",
        "combination": "first passing entry variant in efficiency/slope5/oi order, combined with stop_reentry only if it independently passes",
        "retune_after_result": False,
        "locked_test_read": False,
    }
    intent_path = HERE / "frequency_followup_intent.json"
    if intent_path.exists():
        old = json.loads(intent_path.read_text())
        intent["created_utc"] = old["created_utc"]
        assert old == intent
    else:
        save(intent_path, intent)
    calibration_path = (
        ROOT / "research_outputs/2026-09/slope_band_review/training_calibration.json"
    )
    old = json.loads(calibration_path.read_text())
    ev = old["source_evidence"]
    assert file_sha256(ev["cache"]) == ev["cache_sha256"]
    selected = {(r["date"], r["contract"]) for r in old["selected_representatives"]}
    values = []
    for key, minutes, records in read_frames(ev["cache"], ev["cache_key"]):
        if minutes != 5:
            continue
        for i, row in enumerate(records):
            assert row["day"] < "2026-09-24"
            if (row["day"], key) not in selected or row["volume"] <= 0 or i < 3:
                continue
            a, b, atr = records[i - 3]["ma20"], row["ma20"], row["previous_atr"]
            if any(v is None or not math.isfinite(v) for v in (a, b, atr)) or atr <= 0:
                continue
            magnitude = abs(b - a) / (3 * atr)
            if magnitude > 0:
                values.append(magnitude)
    array = np.asarray(values, dtype="<f8")
    assert len(array) == old["populations"]["5m"]["count"]
    assert (
        hashlib.sha256(array.tobytes()).hexdigest()
        == old["populations"]["5m"]["ordered_values_sha256"]
    )
    low, high = np.quantile(array, [0.1, 0.9], method="linear")
    band = copy.deepcopy(old["strategy_delta"]["slope_band"])
    band["timeframes"]["5m"].update(
        min_atr_per_bar=float(low), max_atr_per_bar=float(high)
    )
    training = {
        "intent_sha256": file_sha256(intent_path),
        "original_calibration": str(calibration_path),
        "original_calibration_sha256": file_sha256(calibration_path),
        "source_cache_sha256": ev["cache_sha256"],
        "population_count": len(array),
        "population_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
        "quantiles": {"0.1": float(low), "0.9": float(high)},
        "slope_band": band,
        "training_cutoff": "2026-09-11",
        "pnl_used": False,
        "locked_test_read": False,
    }
    save(OUT / "slope5_training.json", training)
    oldplan = json.loads((HERE / "optimization_plan.json").read_text())
    report = json.loads((OUT.parent / "optimization_report_data.json").read_text())
    baselines = {}
    for s in report["scenarios"]:
        if s["variant"] != "combined":
            continue
        run = Path(s["directory"])
        baselines[s["month"]] = {
            "directory": str(run),
            "config_sha256": file_sha256(run / "config_snapshot.json"),
            "trades_sha256": file_sha256(run / "trades.csv.gz"),
            "manifest_sha256": file_sha256(run / "manifest.json"),
        }
    variants = {
        "control": {},
        "efficiency": {"efficiency_min": 0.35},
        "slope5": {"slope_band": band},
        "oi": {"enable_oi_filter": False},
        "stop_reentry": {"block_same_day_reentry_after_stop": True},
    }
    for name in ["efficiency", "slope5", "oi"]:
        variants[name + "_reentry"] = variants[name] | variants["stop_reentry"]
    plan = {
        "schema": 1,
        "kind": "sequential_optimization_diagnostic",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "intent": str(intent_path),
        "intent_sha256": file_sha256(intent_path),
        "training": str(OUT / "slope5_training.json"),
        "training_sha256": file_sha256(OUT / "slope5_training.json"),
        "baselines": baselines,
        "order": intent["order"],
        "variants": variants,
        "output": str(OUT),
        "budget": oldplan["budget"],
        "promotion": {
            "total_net_improves": True,
            "each_window_net_not_worse": True,
            "each_window_drawdown_not_worse": True,
            "without_best_trade_net_not_worse": True,
            "entry_variant_trade_count_must_increase": True,
            "stop_variant_may_reduce_trade_count": True,
        },
        "conditional_combination": intent["combination"],
        "locked_test_read": False,
        "sample_status": "retrospective_diagnostic_on_already_inspected_windows",
        "new_holdout_results_claimed": False,
        "parameter_search": False,
        "retune_after_result": False,
    }
    save(TARGET, plan)
    print(
        json.dumps(
            {
                "plan": str(TARGET),
                "slope5_training": training["quantiles"],
                "training_observations": len(array),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
