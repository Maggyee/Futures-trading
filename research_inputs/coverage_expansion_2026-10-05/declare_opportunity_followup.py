"""Freeze the ordered opportunity/exit recipes before their replay results exist."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from research.data import file_sha256
from research.storage import SpaceBudget

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[1]
TARGET = HERE / "opportunity_followup_plan.json"


def main():
    if TARGET.exists():
        raise RuntimeError("已存在冻结声明，不覆盖")
    previous = json.loads((HERE / "frequency_followup_plan.json").read_text())
    replacement = {"candidate_replacement": {
        "policy": "skip_known_zero_capacity",
        "preserve_original_rank": True,
        "refresh": "completed_minute",
        "preserve_held_and_pending_slots": True,
    }}
    trend = {"entry_mode": "pullback_ma10", "trend_entry": {
        "quality_minutes": 5,
        "valid_minutes": 5,
        "require_touch_after_armed": True,
        "cancel_on_invalid_higher_trend": True,
        "efficiency_min": 0.45,
        "remove_low_period_slope_gate": True,
    }}
    exits = {
        "breakeven15": {"breakeven": {"activation_r": 1.5, "include_costs": True}},
        "ma40confirm": {"ma40_exit_confirmation_bars": 2},
    }
    variants = {"control": {}, "replacement": replacement, "trend": trend, **exits,
                "replacement_trend": replacement | trend}
    for entry in ("replacement", "trend", "replacement_trend"):
        for exit_name, delta in exits.items():
            variants[entry + "_" + exit_name] = variants[entry] | delta
    for month, record in previous["baselines"].items():
        run = Path(record["directory"])
        for name, field in (("config_snapshot.json", "config_sha256"),
                            ("trades.csv.gz", "trades_sha256"),
                            ("manifest.json", "manifest_sha256")):
            assert file_sha256(run / name) == record[field], (month, name)
    plan = {
        "schema": 1, "kind": "sequential_optimization_diagnostic",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baselines": copy.deepcopy(previous["baselines"]),
        "order": ["control", "replacement", "trend", "breakeven15", "ma40confirm"],
        "variants": variants,
        "labels": {"control": "原方案", "replacement": "资金可成交候选补位",
                   "trend": "高周期趋势＋1分钟回踩", "breakeven15": "保本启动延后至1.5R",
                   "ma40confirm": "MA40离场连续两分钟确认"},
        "output": str(ROOT / "research_outputs/coverage_expansion_2026-10-05/opportunity_followup"),
        "budget": previous["budget"], "k": 2, "maximum_runs": 21,
        "promotion": previous["promotion"],
        "selection": {
            "entry_priority": ["replacement", "trend"],
            "both_entry_pass": "run replacement_trend; retain only if it passes against control and both individual variants",
            "exit_priority": ["breakeven15", "ma40confirm"],
            "entry_exit_combination": "only if each independently passes; retain only if combination passes against control and selected entry",
            "no_entry_pass": "report frequency issue unresolved; never retune these recipes from their PnL",
        },
        "replacement_scope": "Same pool, R8 direction and original ranking; skip only assessed zero minimum-lot capacity; unknown execution/ATR retains its original rank slot; never skip a candidate merely for failing market filters",
        "trend_scope": "Efficiency and 11-close activity/displacement move to completed 5m bars; keep original 5m slope range and 5/15m trend rules; remove 1m slope gate; retain 1m price/VWAP/OI/shock/extension/cost and next-open execution",
        "trend_window": "Rearm only on a new completed 5m candle, valid for five wall-clock minutes in the same session; MA10 touch must be completed at or after arming; confirmation closes beyond previous high/low; each touch consumed once",
        "exit_scope": "Initial hard stop, time/session exits, costs, slippage, allocation and original target-triggered 2ATR trail remain; only declared break-even R or consecutive MA40 confirmation changes",
        "sample_status": "retrospective_diagnostic_on_already_inspected_windows",
        "locked_test_read": False, "live_trading_changed": False,
        "parameter_search": False, "retune_after_result": False,
        "new_holdout_results_claimed": False,
    }
    budget = SpaceBudget(plan["budget"])
    budget.check(TARGET, reserve=16384)
    Path(plan["output"]).mkdir(exist_ok=True)
    TARGET.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"plan": str(TARGET), "sha256": file_sha256(TARGET),
                      "maximum_runs": plan["maximum_runs"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
