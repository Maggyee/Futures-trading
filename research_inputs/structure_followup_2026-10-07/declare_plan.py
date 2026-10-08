"""Freeze the finite entry-confirmation and structural-protection round."""

import json
from datetime import datetime, timezone
from pathlib import Path

from research.data import file_sha256
from research.storage import SpaceBudget, write_bounded_json

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[1]


def main():
    target = HERE / "plan.json"
    if target.exists():
        raise RuntimeError("冻结声明已存在，不覆盖")
    old = ROOT / "research_outputs/coverage_expansion_2026-10-05/ordered_opportunity"
    previous = json.loads((ROOT / "research_inputs/coverage_expansion_2026-10-05/ordered_opportunity_plan.json").read_text())
    baselines = {}
    for month in ("2026-07", "2026-08", "2026-09"):
        pointer = json.loads((old / f"{month}_control_latest.json").read_text())
        run = Path(pointer["directory"])
        proof = json.loads((run / "independent_ordered_opportunity_audit.json").read_text())
        if pointer["status"] != "completed" or proof["status"] != "passed":
            raise RuntimeError("原7笔研究版未通过来源核对")
        baselines[month] = {"directory": str(run), **{
            key: file_sha256(run / name) for key, name in (
                ("config_sha256", "config_snapshot.json"), ("trades_sha256", "trades.csv.gz"),
                ("manifest_sha256", "manifest.json"), ("audit_sha256", "independent_ordered_opportunity_audit.json"))}}
    entry = {"entry_confirmation": {
        "quality_minutes": 5, "valid_minutes": 5, "breakout_lookback_bars": 2,
        "confirmation_bars": 2, "confirmation": "close_beyond_setup_extreme",
        "pullback_reference": "ma10", "preserve_original_channel": True,
        "higher_efficiency_min": 0.45, "require_touch_start_after_armed": True,
    }}
    protection = {"structure_protection": {
        "timeframe_minutes": 5, "lookback_bars": 3, "buffer_ticks": 1,
        "max_stop_atr": 2.0, "same_session_only": True,
        "retain_original_distance_floors": True, "frozen_after_fill": True,
    }}
    budget = previous["budget"] | {"roots": previous["budget"]["roots"] + [
        str(HERE), str(ROOT / "research_outputs/structure_followup_2026-10-07")]}
    plan = {
        "schema": 1, "kind": "sequential_optimization_diagnostic",
        "created_utc": datetime.now(timezone.utc).isoformat(), "baselines": baselines,
        "order": ["control", "confirmation", "structure"],
        "variants": {"control": {}, "confirmation": entry, "structure": protection,
                     "confirmation_structure": entry | protection},
        "labels": {"control": "当前7笔研究版", "confirmation": "原通道＋突破／回踩延续确认",
                   "structure": "仅5分钟结构止损", "confirmation_structure": "确认入场＋结构止损"},
        "output": str(ROOT / "research_outputs/structure_followup_2026-10-07"),
        "audit_filename": "independent_structure_audit.json", "budget": budget,
        "k": 2, "maximum_runs": 12, "promotion": previous["promotion"],
        "entry_minimum_new_contract_day_directions": 1,
        "entry_active_days_not_worse": True,
        "selection": {"combination": "only if both independently pass; compare against control and both individuals",
                      "failure": "retain current seven-trade research recipe; preserve all failures; do not retune"},
        "entry_scope": "Preserve original direct edge and fixed candidates. A separate channel qualifies using the original completed 5m/15m trend, 5m slope band and 5m efficiency/activity/displacement. Replace only its 1m efficiency/activity/displacement/slope gates. Within a five-minute window, a completed candle closes beyond the previous two highs/lows (breakout), or confirms a post-qualification completed MA10 touch (pullback); the immediately adjacent next candle must close beyond the setup candle high/low. Reject stale, nonadjacent or cross-session confirmation. Original channel has priority; share capital, price/cost checks, cooldown and stop-reentry. Consume a submitted setup once.",
        "protection_scope": "Use the last three completed 5m candles of the current continuous session: minimum low for long / maximum high for short, plus one adverse tick. At signal and fill, stop distance is max(original training floor, existing 1m ATR and cost floors, distance to the frozen structure, signal reservation floor). Require positive geometry and distance <=2 times the signal-time known 5m ATR; cancel otherwise. Reallocate quantity within original 0.2% single-trade and all other caps. Structure is never updated after fill; hard protection never waits for confirmation. Preserve 1R cost-covered break-even and 2ATR target-activated trail.",
        "threshold_basis": "Finite initial research recipe fixed before replay, not fitted on this round's PnL. Reuses original frozen slope/quality thresholds; no new quantile calculation or parameter search.",
        "sample_status": "retrospective_diagnostic_on_already_inspected_windows",
        "locked_test_read": False, "live_trading_changed": False, "parameter_search": False,
        "retune_after_result": False, "new_holdout_results_claimed": False,
        "prior_review_sha256": file_sha256(ROOT / "research_outputs/strategy_recheck_2026-10-07/diagnosis.json"),
    }
    guard = SpaceBudget(budget)
    guard.check(target, reserve=80000)
    Path(plan["output"]).mkdir(parents=True, exist_ok=True)
    write_bounded_json(target, plan, guard)
    print(json.dumps({"plan": str(target), "sha256": file_sha256(target), "maximum_runs": 12,
                      "space": guard.check(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
