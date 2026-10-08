"""Replay the frozen price-confirmation and structural-stop stages in order."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .opportunity_followup import ROOT, BaselineEntries
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


def run(month, variant):
    os.umask(0o077)
    return run_replay(month, variant, plan_path=PLAN,
                      engine_factory=StructureBacktest, entries_factory=StructureEntries)


def stage(variant):
    plan = json.loads(PLAN.read_text())
    for month in sorted(plan["baselines"]):
        subprocess.run([sys.executable, "-m", "research.structure_followup", "run",
                        "--month", month, "--variant", variant], check=True)
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
    parser.add_argument("action", choices=("run", "stage", "all"))
    parser.add_argument("--month")
    parser.add_argument("--variant")
    args = parser.parse_args()
    if args.action == "all":
        all_stages()
    elif args.action == "stage":
        stage(args.variant)
    else:
        run(args.month, args.variant)
