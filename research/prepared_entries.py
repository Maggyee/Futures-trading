"""Stream verified causal entry evaluations while recomputing portfolio state."""

import csv
import gzip
import hashlib
import json
import math
import sys
import tarfile
from pathlib import Path

from .config import ResearchError
from .data import file_sha256


def shared_keys(pairs):
    # JSON archives repeat the same small set of field names on every minute.
    return {sys.intern(key): value for key, value in pairs}


class PreparedEntries:
    def __init__(self, run, cfg, prepared_evidence, *, allow_efficiency_change=False, allow_candidate_expansion=False, allow_optimization_overlay=False, allow_selected_projection=False):
        self.run, self.cfg = Path(run), cfg
        self.allow_efficiency_change = allow_efficiency_change
        self.allow_candidate_expansion = allow_candidate_expansion
        old = json.loads((self.run / "config_snapshot.json").read_text())
        old_scope = old.get("storage", {}).get("record_unselected_signals", True)
        new_scope = cfg.get("storage", {}).get("record_unselected_signals", True)
        self.selected_projection = allow_selected_projection and old_scope and not new_scope
        if self.selected_projection and (allow_candidate_expansion or type(old["strategy"].get("k")) is not int or old["strategy"]["k"] != cfg["strategy"].get("k")):
            raise ResearchError("投影仅可省略未选候选，必须保持原K")
        if new_scope != old_scope and not self.selected_projection:
            raise ResearchError("复用入场快照必须保持原候选观察记录范围")
        self.old_strategy = old["strategy"]
        allowed = {"trailing_exit"} | ({"efficiency_min"} if allow_efficiency_change else set()) | ({"k"} if allow_candidate_expansion else set())
        if allow_optimization_overlay:
            from .optimization_declaration import validate_optimization
            validate_optimization(cfg)
            allowed |= {"entry_cost_filter", "breakeven"}
        if {k: v for k, v in cfg["strategy"].items() if k not in allowed} != {k: v for k, v in old["strategy"].items() if k not in allowed}:
            raise ResearchError("复用入场快照只能改变追踪退出或显式声明的效率门槛/候选数量")
        if allow_candidate_expansion:
            before, after = old["strategy"]["k"], cfg["strategy"]["k"]
            if type(before) is not int or type(after) is not int or not 1 <= before <= after <= 10 or allow_efficiency_change:
                raise ResearchError("候选扩展必须为1—10的整数且保持入场质量门槛")
        if allow_efficiency_change:
            thresholds = [s.get("efficiency_min") for s in (old["strategy"], cfg["strategy"])]
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 1 for v in thresholds) or not cfg["strategy"].get("enable_smooth_filter"):
                raise ResearchError("重算效率门槛要求有限的(0,1]阈值并保持平滑过滤开启")
        for key in ("risk", "metadata", "calendar", "splits", "execution", "calibration_snapshot"):
            if cfg[key] != old[key]:
                raise ResearchError("复用入场快照不能改变：" + key)
        manifest = json.loads((self.run / "manifest.json").read_text())
        if manifest["data_fingerprint"] != prepared_evidence["retained_data_fingerprint"]:
            raise ResearchError("复用入场快照的数据指纹不同")
        expected_window = cfg.get("archive_evaluation", {}).get("window", cfg["splits"]["validation"]) if allow_optimization_overlay else cfg["splits"]["validation"]
        expected_split = "retrospective" if allow_optimization_overlay and cfg.get("archive_evaluation") else "validation"
        if manifest["window"] != expected_window or manifest["split"] != expected_split or manifest["scope"] != "shared":
            raise ResearchError("复用入场快照仅限原validation/shared窗口")
        sources = {}
        with tarfile.open(self.run / "source_snapshot.tar.gz", "r:gz") as archive:
            for name in ("signals.py", "refinements.py"):
                current = Path(__file__).with_name(name).read_bytes()
                archived = archive.extractfile("research/" + name).read()
                if current != archived:
                    raise ResearchError("入场/指标算法改变，不能复用快照：" + name)
                sources[name] = hashlib.sha256(current).hexdigest()
        self.path = self.run / "signals.csv.gz"
        self.evidence = {
            "source_run": str(self.run), "source_signals_sha256": file_sha256(self.path),
            "unchanged_entry_sources": sources, "data_fingerprint": manifest["data_fingerprint"],
            "fields_reused": ["filters_except_state_and_efficiency" if allow_efficiency_change else "filters_except_state", "snapshot", "pullback", "exit_flags"],
            "efficiency_filter_recomputed_from_causal_snapshot": allow_efficiency_change,
            "candidate_filter_recomputed_from_original_rank": allow_candidate_expansion,
            "state_triggers_execution_and_cash_recomputed": True,
            "source_fill_or_profit_used": False, "locked_test_read": False,
        }
        self.count, self.order, self.efficiency_changes, self.candidate_changes = 0, None, 0, 0
        self.projection_skipped = 0

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
        row = self.next_selected()
        if row is None:
            raise ResearchError("入场快照提前结束")
        identity = {"time": bar.end.isoformat(), "date": bar.trading_day, "contract": bar.key, "direction": candidate["direction"], "rank": str(candidate["rank"])}
        if any(row[k] != value for k, value in identity.items()):
            raise ResearchError("入场快照的时间、合约或排名顺序改变")
        def decode(field):
            return json.loads(row[field], object_pairs_hook=shared_keys)
        filters, snapshot = decode("filters"), decode("snapshot")
        if filters["entry_time"] != before_cutoff:
            raise ResearchError("入场时间边界发生改变")
        flags = decode("exit_flags")
        pullback = decode("pullback") if row["pullback"] else None
        if self.order is None:
            fresh = self.original_logic.evaluate(bar, candidate, state_allows, before_cutoff)
            self.order = list(fresh["filters"])
            recomputed = {"state"} | ({"efficiency"} if self.allow_efficiency_change else set()) | ({"candidate"} if self.allow_candidate_expansion else set())
            if fresh["snapshot"] != snapshot or fresh["exit_flags"] != flags or fresh["pullback"] != pullback or any(fresh["filters"][k] != filters[k] for k in filters if k not in recomputed):
                raise ResearchError("首个重新计算的入场快照与来源不一致")
        if set(filters) != set(self.order):
            raise ResearchError("入场过滤字段改变")
        if self.allow_candidate_expansion:
            original = candidate["rank"] <= self.old_strategy["k"]
            current = candidate["rank"] <= self.cfg["strategy"]["k"]
            if filters["candidate"] != original or candidate["selected"] != current:
                raise ResearchError("候选选择与原排名/声明K不符")
            self.candidate_changes += original != current
            filters["candidate"] = current
        if self.allow_efficiency_change:
            value = snapshot["efficiency"]
            valid = value is not None and math.isfinite(value)
            original = bool(valid and value >= self.old_strategy["efficiency_min"])
            if filters["efficiency"] != original:
                raise ResearchError("原效率过滤与来源快照不符")
            current = bool(valid and value >= self.cfg["strategy"]["efficiency_min"])
            self.efficiency_changes += original != current
            filters["efficiency"] = current
        filters = {k: state_allows if k == "state" else filters[k] for k in self.order}
        self.count += 1
        return {"filters": filters, "rejections": [k for k, v in filters.items() if not v], "all_pass": all(filters.values()), "pullback": pullback, "snapshot": snapshot, "exit_flags": flags}

    def finish(self):
        if self.next_selected() is not None:
            raise ResearchError("有未回放的入场观察，拒绝省略行情")
        self.evidence["observations_replayed"] = self.count
        self.evidence["efficiency_filter_changes"] = self.efficiency_changes
        self.evidence["candidate_filter_changes"] = self.candidate_changes
        self.evidence["unselected_source_observations_projected_out"] = self.projection_skipped

    def next_selected(self):
        row = next(self.reader, None)
        while row is not None and self.selected_projection and int(row["rank"]) > self.old_strategy["k"]:
            if json.loads(row["filters"])["candidate"] is not False:
                raise ResearchError("未选候选投影与原过滤不一致")
            self.projection_skipped += 1
            row = next(self.reader, None)
        return row
