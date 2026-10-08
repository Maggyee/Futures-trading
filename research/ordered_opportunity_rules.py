"""Executable candidate pool and two entry channels with shared protection."""

from collections import defaultdict

from .opportunity_rules import OpportunityBacktest, TrendWindowLogic


class DualChannelLogic:
    def __init__(self, original, baseline):
        self.baseline = baseline
        self.trend = TrendWindowLogic(original, lambda *unused: self.direct)

    def evaluate(self, *args):
        self.direct = self.baseline(*args)
        pullback = self.trend.evaluate(*args)
        # Cost and stop-reentry gates are common to both channels and are added
        # by the portfolio engine before entry_trigger selects the actual rule.
        return {**self.direct, "_entry_channels": {"direct": self.direct, "pullback": pullback},
                "_channel_time": args[0].end.isoformat()}


class OrderedOpportunityBacktest(OpportunityBacktest):
    extra_journals = ("candidate_selection", "exit_confirmation", "entry_channels")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry_channels = []

    def pool_check(self, candidate, bar, cutoff):
        assessment = self.assess_capacity(candidate, bar, cutoff)
        if not assessment["assessed"]:
            return assessment | {"action": "skip_unavailable"}
        distance = (assessment["roundtrip_fees_per_lot"] / assessment["value_per_price"]
                    + 2 * self.cfg["strategy"]["slippage_ticks"] * assessment["tick_size"])
        ratio = distance / assessment["atr_previous"]
        accepted_cost = ratio <= self.cfg["strategy"]["entry_cost_filter"]["max_cost_atr"] + 1e-12
        action = ("skip_zero_capacity" if assessment["quantity"] == 0 else
                  "select" if accepted_cost else "skip_cost")
        return assessment | {"action": action, "cost_atr": ratio, "accepted_cost": accepted_cost}

    def update_candidates(self, candidates, current, day, cutoff):
        if not self.cfg["strategy"].get("candidate_pool"):
            return super().update_candidates(candidates, current, day, cutoff)
        cohorts = defaultdict(list)
        for candidate in candidates.values():
            cohorts[(candidate["group"], candidate["direction"])].append(candidate)
        selected = set()
        for (group, direction), cohort in sorted(cohorts.items()):
            ordered = sorted(cohort, key=lambda c: c["rank"])
            retained = [c for c in ordered if self.state(c["contract"]).position
                        or self.state(c["contract"]).name == "ENTRY_PENDING"]
            chosen = {c["contract"] for c in retained}
            checks = [{"contract": c["contract"], "rank": c["rank"],
                       "action": "retain_held_or_reserved"} for c in retained]
            for candidate in ordered:
                if len(chosen) >= self.cfg["strategy"]["k"]:
                    break
                key = candidate["contract"]
                if key in chosen:
                    continue
                assessment = self.pool_check(candidate, current.get(key), cutoff)
                checks.append({"contract": key, "rank": candidate["rank"], **assessment})
                if assessment["action"] == "select":
                    chosen.add(key)
            selected.update(chosen)
            risk, margin, slots, usage = self.allocator.usage(self.states)
            self.candidate_selection.append({
                "trigger": False, "time": cutoff.isoformat(), "date": day,
                "group": group, "direction": direction, "selected": sorted(chosen),
                "checks": checks, "equity": self.equity_value(), "risk_used": risk,
                "margin_used": margin, "slots_used": slots,
                "group_usage": usage.get(group, {"risk": 0, "margin": 0}),
            })
        for key, candidate in list(candidates.items()):
            passing = key in selected
            if passing != candidate["selected"]:
                self.state(key).previous_pass = False
            candidates[key] = candidate | {"selected": passing}

    def entry_trigger(self, key, evaluation):
        if not self.cfg["strategy"].get("dual_entry"):
            return super().entry_trigger(key, evaluation)
        channels = evaluation.pop("_entry_channels")
        signal_time = evaluation.pop("_channel_time")
        cost_check = evaluation.get("cost_check")
        common = {k: v for k, v in evaluation["filters"].items() if k in {"cost", "stop_reentry"}}
        direct, pullback = channels["direct"], channels["pullback"]
        passing = {name: all(result["filters"].values()) and all(common.values())
                   for name, result in channels.items()}
        st = self.state(key)
        previous = st.previous_pass
        event = pullback["pullback"]
        consumed = event is not None and event["event"] in st.consumed
        triggers = {"direct": passing["direct"] and not previous,
                    "pullback": passing["pullback"] and event is not None and not consumed}
        chosen = ("direct" if triggers["direct"] else "pullback" if triggers["pullback"] else
                  "direct" if passing["direct"] else "pullback" if passing["pullback"] else "direct")
        actual = direct if chosen == "direct" else pullback
        filters = actual["filters"] | common
        evaluation.clear()
        evaluation.update(actual, filters=filters, all_pass=all(filters.values()),
                          rejections=[k for k, v in filters.items() if not v],
                          snapshot=actual["snapshot"] | {"entry_channel": chosen})
        if cost_check is not None:
            evaluation["cost_check"] = cost_check
        # Keep direct-channel edge detection independent of pullback eligibility.
        st.previous_pass = passing["direct"]
        if any(passing.values()) or any(triggers.values()):
            self.entry_channels.append({
                "trigger": False, "time": signal_time, "contract": key,
                "chosen": chosen, "direct_pass": passing["direct"],
                "pullback_pass": passing["pullback"], "direct_trigger": triggers["direct"],
                "pullback_trigger": triggers["pullback"], "previous_direct_pass": previous,
                "pullback_consumed": consumed, "pullback": event,
                "direct_filters": direct["filters"], "pullback_filters": pullback["filters"],
                "trend_entry": pullback["snapshot"]["trend_entry"], "common_filters": common,
            })
        return triggers[chosen]

    def opportunity_priority(self, opportunity):
        original = super().opportunity_priority(opportunity)
        if not self.cfg["strategy"].get("dual_entry"):
            return original
        return (opportunity[0]["snapshot"]["entry_channel"] != "direct", *original)

    def run(self, *args, **kwargs):
        result = super().run(*args, **kwargs)
        result["entry_channels"] = self.entry_channels
        return result
