"""Replay the frozen price-confirmation and structural-stop stages in order."""

import argparse
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .opportunity_followup import ROOT, BaselineEntries, configuration
from .opportunity_followup import run as run_replay
from .structure_rules import ConfirmedLogic, StructureBacktest

PLAN = ROOT / "research_inputs/structure_followup_2026-10-07/plan.json"


class StructureEntries(BaselineEntries):
    def bind(self, original):
        self.original = original
        self.data, self.features = original.data, original.features
        enabled = any(self.cfg["strategy"].get(k) for k in ("entry_confirmation", "structure_protection"))
        self.trend = ConfirmedLogic(original, self.baseline, []) if enabled else None
        return self

    def evaluate(self, bar, candidate, *args):
        result = super().evaluate(bar, candidate, *args)
        if self.cfg.get("rule_layer_review"):
            result["_observation"] = (bar, candidate)
        return result


def run(month, variant, plan_path=PLAN, prepared=None):
    os.umask(0o077)
    return run_replay(month, variant, plan_path=plan_path, prepared=prepared,
                      engine_factory=StructureBacktest, entries_factory=StructureEntries)


LAYER_PLAN = ROOT / "research/rule_layer_plan.json"


def declare_layers():
    """Declare a finite matrix before reading its results; never overwrite it."""
    from .data import file_sha256
    from .reporting import write_json

    if LAYER_PLAN.exists():
        raise ValueError("本轮声明已存在，不覆盖")
    old = json.loads(PLAN.read_text())
    entry = copy.deepcopy(old["variants"]["confirmation"]["entry_confirmation"])
    lifetime = entry | {"state_policy": "setup_lifetime", "patterns": ["breakout", "pullback"],
                        "pullback_confirmation": "next_close"}
    recovery = lifetime | {"patterns": ["pullback"], "pullback_confirmation": "recovery_close"}
    stop = copy.deepcopy(old["variants"]["structure"])
    base_profit = {"basis": "signal_base_price_scale", "breakeven_multiple": 1.,
                   "target_multiple": 1., "trailing_atr_multiple": 2.}
    wider_trail = base_profit | {"trailing_atr_multiple": 3.}
    variants = {"control": {}, "bucket_confirmation": {"entry_confirmation": entry},
                "lifetime": {"entry_confirmation": lifetime}, "pullback_recovery": {"entry_confirmation": recovery},
                "structure_coupled": stop, "stop_only": stop | {"profit_protection": base_profit},
                "profit_only": {"profit_protection": wider_trail}, "stop_profit": stop | {"profit_protection": wider_trail}}
    output = ROOT / "research_outputs/rule_layers_2026-10-08"
    plan = {"schema": 2, "kind": old["kind"], "created_utc": datetime.now(timezone.utc).isoformat(),
            "baselines": old["baselines"], "variants": variants, "order": list(variants),
            "labels": dict(zip(variants, ["原7笔基准", "旧确认规则及取消诊断", "形态自身有效期", "回踩恢复即确认",
                                         "结构止损与原联动", "仅改结构止损", "仅改3ATR追踪距离", "结构止损＋独立3ATR追踪"], strict=True)),
            "output": str(output), "audit_filename": "independent_structure_audit.json",
            "budget": old["budget"] | {"roots": old["budget"]["roots"]+[str(output)], "max_bytes": 6*1024**3},
            "maximum_runs": 24, "k": 2, "sample_status": old["sample_status"],
            "locked_test_read": False, "live_trading_changed": False, "parameter_search": False,
            "retune_after_result": False, "new_holdout_results_claimed": False,
            "rule_layer_review": {"recompute_all_filters": True, "cost_filter_changed": False},
            "engineering": ["causal completed data", "exact control ledger", "all pattern terminal causes",
                            "price and cost rechecked at fill", "unchanged integer risk and margin limits", "source fingerprints"],
            "research_criteria": {"max_window_drawdown_cny": 10000, "max_window_loss_cny": 10000,
                "basis": "original 1% portfolio risk budget of 1000000 initial capital; research guardrail, not a loss guarantee",
                "minimum_trades_for_further_review": 20, "automatic_promotion": False,
                "zero_trade_window": "use absolute budgets; no requirement to dominate zero drawdown",
                "required_diagnostics": ["window net and drawdown", "deduplicated patterns and terminal causes",
                    "channel net and rejected accounts", "four cost scales", "non-LC contribution", "without top one and two",
                    "net per lot and initial-price-risk R", "risk budget utilization", "fixed-path extra tick cost stress"]},
            "profit_scope": "Freeze the unstructured signal-time stop and target floors as profit price references. Widening the structural stop only changes initial risk and integer quantity; it cannot move those profit references. Separately compare 2ATR with predeclared 3ATR trailing distance; never keep the old lots by enlarging risk.",
            "cost_scope": "Keep cost/1m ATR <=0.5. Record 5m ATR, original predeclared target space and initial risk ratios without assigning new cutoffs; space is a mechanical training/causal-floor proxy, not a forecast.",
            "prior_plan_sha256": file_sha256(PLAN)}
    write_json(LAYER_PLAN, plan)
    print(json.dumps({"plan": str(LAYER_PLAN), "sha256": file_sha256(LAYER_PLAN), "maximum_runs":24},ensure_ascii=False))
    return plan


def batch(month, plan_path):
    """Reuse numeric frames while retaining cropped training proofs per config."""
    from .data import file_sha256, load_data
    from .frequency_followup import require
    from .signals import Features
    from .structure_audit import audit

    plan, parent, cfg = configuration(month,"control",plan_path)
    window = json.loads((parent/"manifest.json").read_text())["window"]
    # September's cropped PreparedDataset carries the independently verified
    # full training fingerprint. A config change in scope_data would discard
    # that proof; let run() prepare it against each variant's exact config.
    prepared = None
    if month != "2026-09":
        data = load_data(cfg,cutoff=window["end"])
        require(data.fingerprint == json.loads((parent/"manifest.json").read_text())["data_fingerprint"], "历史行情指纹不符")
        features = Features(data,cfg["storage"]["indicator_cache_root"])
        cache = Path(cfg["storage"]["indicator_cache_root"])/(features.cache_key+".jsonl.gz")
        prepared = data, features, {"source_run":str(parent),"retained_data_fingerprint":data.fingerprint,
            "cache":str(cache),"cache_key":features.cache_key,"cache_sha256":file_sha256(cache),
            "feature_algorithm_and_accessors_unchanged":True,"locked_test_read":False}
    for variant in plan["order"]:
        record = run(month,variant,plan_path,prepared)
        if variant == "control":
            audit(record["directory"])


def layers(plan_path):
    from .data import file_sha256
    from .experiments import code_identity
    from .reporting import write_json
    from .structure_assessment import assess_layers

    plan = json.loads(Path(plan_path).read_text())
    freeze_path = Path(plan_path).with_name("implementation_freeze.json")
    freeze = {"plan_sha256":file_sha256(plan_path), "strategy_and_runner_hashes":code_identity()["source_hashes"]}
    if freeze_path.exists():
        if json.loads(freeze_path.read_text()) != freeze:
            raise ValueError("本轮实现冻结已改变；保留原输出，另立版本")
    else:
        write_json(freeze_path,freeze)
    for month in sorted(plan["baselines"]):
        subprocess.run([sys.executable,"-m","research.structure_followup","batch","--plan",str(plan_path),"--month",month],check=True)
        for variant in plan["order"]:
            pointer = Path(plan["output"])/(month+"_"+variant+"_latest.json")
            run_path = Path(json.loads(pointer.read_text())["directory"])
            if not (run_path/plan["audit_filename"]).exists():
                subprocess.run([sys.executable,"-m","research.structure_audit","--directory",str(run_path)],check=True)
        assess_layers(plan_path,final=False)
    return assess_layers(plan_path,final=True)


def stage(variant, plan_path=PLAN):
    plan = json.loads(Path(plan_path).read_text())
    for month in sorted(plan["baselines"]):
        subprocess.run([sys.executable, "-m", "research.structure_followup", "run",
                        "--month", month, "--variant", variant, "--plan", str(plan_path)], check=True)
        pointer = Path(plan["output"]) / (month + "_" + variant + "_latest.json")
        record = json.loads(pointer.read_text())
        subprocess.run([sys.executable, "-m", "research.structure_audit", "--directory",
                        record["directory"]], check=True)


def all_stages():
    from .structure_assessment import assess

    plan = json.loads(PLAN.read_text())
    for variant in plan["order"]:
        stage(variant)
        assess()
    assessment = assess()
    if all(assessment["promotion"][v]["passed"] for v in ("confirmation", "structure")):
        stage("confirmation_structure")
    assess(final=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "stage", "all", "declare-layers", "layers", "batch"))
    parser.add_argument("--plan",type=Path)
    parser.add_argument("--month")
    parser.add_argument("--variant")
    args = parser.parse_args()
    if args.action == "declare-layers":
        declare_layers()
    elif args.action == "layers":
        layers((args.plan or LAYER_PLAN).resolve())
    elif args.action == "batch":
        batch(args.month,(args.plan or LAYER_PLAN).resolve())
    elif args.action == "all":
        if args.plan and args.plan.resolve() != PLAN.resolve():
            parser.error("新四层矩阵使用layers；all仅支持原阶段保留标准")
        all_stages()
    elif args.action == "stage":
        stage(args.variant,(args.plan or PLAN).resolve())
    else:
        run(args.month, args.variant, (args.plan or PLAN).resolve())
