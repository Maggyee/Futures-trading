"""Causal selection, independent entry edges and shared protection boundaries."""

import copy

import pytest

from research.calendar import at
from research.config import ResearchError, validate_config
from research.data import load_data
from research.fixtures import create_fixture
from research.ordered_opportunity_rules import OrderedOpportunityBacktest

DAY = "2026-01-08"


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_ORDERED_ONLY")))


def engine(market):
    cfg = copy.deepcopy(market.cfg)
    cfg.pop("baseline_expectation", None)
    cfg["strategy"]["k"] = 2
    return OrderedOpportunityBacktest(market, cfg)


def evaluation(direct, pullback, *, cost=True, allowed=True, touch="09:15"):
    base = {"filters": {"state": allowed, "entry_time": True, "market": direct},
            "snapshot": {}, "pullback": None, "all_pass": direct and allowed,
            "rejections": [], "exit_flags": []}
    supplement = {"filters": {"state": allowed, "entry_time": True, "higher_trend_quality": pullback},
                  "snapshot": {"trend_entry": {"source_5m_end": at(DAY, "09:15").isoformat()}},
                  "pullback": {"event": at(DAY, touch).isoformat()},
                  "all_pass": pullback and allowed, "rejections": [], "exit_flags": []}
    result = copy.deepcopy(base)
    result["filters"].update(cost=cost, stop_reentry=True)
    result.update(_entry_channels={"direct": base, "pullback": supplement},
                  _channel_time=at(DAY, "09:16").isoformat(), cost_check={"accepted": cost})
    return result


def enable_dual(e):
    e.cfg["strategy"]["dual_entry"] = {"priority": "direct", "shared_capital": True,
                                         "consume_pullback_once": True}


def test_both_channels_trigger_once_and_breakout_keeps_priority(market):
    e = engine(market)
    enable_dual(e)
    row = evaluation(True, True)
    assert e.entry_trigger("A", row)
    assert row["snapshot"]["entry_channel"] == "direct" and row["pullback"] is None
    assert row["cost_check"] == {"accepted": True}
    assert e.state("A").previous_pass is True and e.state("A").consumed == set()


def test_pullback_does_not_hide_a_new_breakout_edge(market):
    e = engine(market)
    enable_dual(e)
    row = evaluation(False, True)
    assert e.entry_trigger("A", row)
    assert row["snapshot"]["entry_channel"] == "pullback"
    assert e.state("A").previous_pass is False
    row = evaluation(True, True)
    assert e.entry_trigger("A", row)
    assert row["snapshot"]["entry_channel"] == "direct"


def test_same_minute_breakouts_reserve_capital_before_other_contract_pullbacks(market):
    e = engine(market)
    enable_dual(e)
    pullback = ({"rank": 1, "r8": .1, "group": "commodity", "contract": "A",
                 "snapshot": {"entry_channel": "pullback"}}, None, 1)
    direct = ({"rank": 2, "r8": .05, "group": "commodity", "contract": "B",
               "snapshot": {"entry_channel": "direct"}}, None, 1)
    assert sorted([pullback, direct], key=e.opportunity_priority) == [direct, pullback]
    e.cfg["strategy"].pop("dual_entry")
    assert sorted([pullback, direct], key=e.opportunity_priority) == [pullback, direct]


@pytest.mark.parametrize("cost,allowed", [(False, True), (True, False)])
def test_common_cost_and_position_gates_block_both_channels(market, cost, allowed):
    e = engine(market)
    enable_dual(e)
    row = evaluation(True, True, cost=cost, allowed=allowed)
    assert not e.entry_trigger("A", row) and not row["all_pass"]


def test_stop_reentry_blocks_the_added_channel(market):
    e = engine(market)
    enable_dual(e)
    row = evaluation(False, True)
    row["filters"]["stop_reentry"] = False
    assert not e.entry_trigger("A", row) and not row["all_pass"]


def test_consumed_pullback_never_retriggers(market):
    e = engine(market)
    enable_dual(e)
    e.state("A").consumed.add(at(DAY, "09:15").isoformat())
    row = evaluation(False, True)
    assert not e.entry_trigger("A", row)
    assert e.entry_channels[-1]["pullback_consumed"] is True


def test_pool_skips_unknown_and_expensive_candidates_without_market_reranking(market):
    e = engine(market)
    e.cfg["strategy"]["candidate_pool"] = {"policy": "executable_affordable_cost"}
    records = {str(i): {"contract": str(i), "rank": i, "selected": i <= 2,
                       "group": "commodity", "direction": "LONG"} for i in range(1, 5)}
    archived = list(records.values())
    e.pool_check = lambda c, b, t: {"action": {1: "skip_unavailable", 2: "skip_cost"}.get(c["rank"], "select")}
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert {k for k, c in records.items() if c["selected"]} == {"3", "4"}
    assert [r["selected"] for r in archived] == [True, True, False, False]
    assert [r["rank"] for r in records.values()] == [1, 2, 3, 4]


def test_pool_keeps_reserved_positions_in_their_slots(market):
    e = engine(market)
    e.cfg["strategy"]["candidate_pool"] = {"policy": "executable_affordable_cost"}
    records = {str(i): {"contract": str(i), "rank": i, "selected": i <= 2,
                       "group": "commodity", "direction": "LONG"} for i in range(1, 5)}
    e.state("4").name = "ENTRY_PENDING"
    e.state("4").pending = {"reserved_risk": 10, "reserved_margin": 10, "group": "commodity"}
    e.pool_check = lambda *unused: {"action": "select"}
    e.update_candidates(records, {}, DAY, at(DAY, "09:16"))
    assert {k for k, c in records.items() if c["selected"]} == {"1", "4"}


def test_candidate_cost_includes_two_sides_of_slippage(market):
    e = engine(market)
    e.cfg["strategy"].update(entry_cost_filter={"max_cost_atr": .5}, slippage_ticks=1)
    e.assess_capacity = lambda *unused: {"assessed": True, "quantity": 1,
        "roundtrip_fees_per_lot": 2, "value_per_price": 10, "tick_size": 1, "atr_previous": 4}
    result = e.pool_check({}, None, at(DAY, "09:16"))
    assert result["cost_atr"] == pytest.approx(.55) and result["action"] == "skip_cost"


def test_invalid_pool_policy_cannot_change_rank_or_cost_limit(market):
    e = engine(market)
    e.cfg["strategy"]["candidate_pool"] = {"policy": "profit_rank"}
    with pytest.raises(ResearchError):
        validate_config(e.cfg)


def test_extended_streamed_journals_are_exported_for_independent_audit(market, tmp_path):
    """A completed run must publish each engine journal, including entry channels."""
    import json

    from research.coverage_audit import rows
    from research.execution import PortfolioBacktest
    from research.experiments import run_one
    from research.signals import Features
    from research.storage import read_result

    class JournalEngine(PortfolioBacktest):
        extra_journals = ("entry_channels",)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.entry_channels = []

        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            self.entry_channels.append({"contract": "TEST", "chosen": "direct",
                                        "direct_filters": {"market": True}, "trigger": False})
            result["entry_channels"] = self.entry_channels
            return result

    cfg = copy.deepcopy(market.cfg)
    cfg.setdefault("storage", {}).update(stream_signals=True, compress_signal_journal=True,
                                          compact_results=True)
    directory, result = run_one(market, cfg, tmp_path / "runs", cfg["splits"]["validation"],
                                prepared_features=Features(market), engine_factory=JournalEngine)
    assert result["status"] == "completed"
    exported = list(rows(directory / "entry_channels.csv.gz"))
    assert len(exported) == 1 and exported[0]["chosen"] == "direct"
    assert json.loads(exported[0]["direct_filters"]) == {"market": True}
    saved = read_result(directory)["entry_channels"]
    assert saved[0]["chosen"] == exported[0]["chosen"]
    for name in ("signals", "entry_channels"):
        result[name].discard()
