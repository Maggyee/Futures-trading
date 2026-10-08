"""Run the executable-pool and dual-entry stages, each in its own process."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .frequency_followup import require
from .opportunity_followup import ROOT, BaselineEntries
from .opportunity_followup import run as run_replay
from .ordered_opportunity_rules import DualChannelLogic, OrderedOpportunityBacktest

PLAN = ROOT / "research_inputs/coverage_expansion_2026-10-05/ordered_opportunity_plan.json"


class OrderedEntries(BaselineEntries):
    def bind(self, original):
        self.original = original
        self.data, self.features = original.data, original.features
        self.trend = DualChannelLogic(original, self.baseline) if self.cfg["strategy"].get("dual_entry") else None
        return self

    def finish(self):
        if self.peek is not None:
            self.skipped += 1 + sum(1 for _ in self.reader)
        if not self.cfg["strategy"].get("candidate_pool"):
            require(self.skipped == self.fresh == 0, "固定候选观察有遗漏")
        self.evidence.update(observations_replayed=self.reused, fresh_observations=self.fresh,
                             unselected_original_observations=self.skipped,
                             first_measurement_checked_contracts=sorted(self.checked))


def run(month, variant):
    from .ordered_opportunity_storage import reuse_archived_bytes

    os.umask(0o077)
    reuse_archived_bytes(json.loads(PLAN.read_text()))
    return run_replay(month, variant, plan_path=PLAN,
                      engine_factory=OrderedOpportunityBacktest, entries_factory=OrderedEntries)


def stage(variant):
    plan = json.loads(PLAN.read_text())
    for month in sorted(plan["baselines"]):
        subprocess.run([sys.executable, "-m", "research.ordered_opportunity", "run",
                        "--month", month, "--variant", variant], check=True)
        pointer = Path(plan["output"]) / (month + "_" + variant + "_latest.json")
        record = json.loads(pointer.read_text())
        subprocess.run([sys.executable, "-m", "research.ordered_opportunity_audit",
                        "--directory", record["directory"]], check=True)


def all_stages():
    from .ordered_opportunity_assessment import assess

    plan = json.loads(PLAN.read_text())
    for variant in plan["order"]:
        stage(variant)
        assess()
    assessment = assess()
    if all(assessment["promotion"][v]["passed"] for v in ("pool", "dual")):
        stage("pool_dual")
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
