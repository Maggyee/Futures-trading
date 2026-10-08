"""Price geometry, causal confirmation boundaries and unchanged capital limits."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from research.calendar import MINUTE, at
from research.config import ResearchError, validate_config
from research.data import load_data
from research.execution import PortfolioBacktest
from research.fixtures import create_fixture
from research.signals import Features
from research.structure_rules import (
    ConfirmedLogic,
    StructureBacktest,
    structure_context,
)
from research.trailing import initial_trailing

DAY, KEY = "2026-01-08", "aa2603.SHFE"
ENTRY = {"quality_minutes": 5, "valid_minutes": 5, "breakout_lookback_bars": 2,
         "confirmation_bars": 2, "confirmation": "close_beyond_setup_extreme",
         "pullback_reference": "ma10", "preserve_original_channel": True,
         "higher_efficiency_min": .45, "require_touch_start_after_armed": True}
STOP = {"timeframe_minutes": 5, "lookback_bars": 3, "buffer_ticks": 1,
        "max_stop_atr": 2., "same_session_only": True,
        "retain_original_distance_floors": True, "frozen_after_fill": True}
LIFETIME = ENTRY | {"state_policy": "setup_lifetime", "patterns": ["breakout", "pullback"],
                    "pullback_confirmation": "next_close"}
PROFIT = {"basis": "signal_base_price_scale", "breakeven_multiple": 1.,
          "target_multiple": 1., "trailing_atr_multiple": 2.}


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_STRUCTURE_ONLY")))


def engine(market):
    return StructureBacktest(market, copy.deepcopy(market.cfg))


def pattern(market, sign=1):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["entry_confirmation"] = ENTRY
    data = SimpleNamespace(cfg=cfg, metadata=market.metadata, calendar=market.calendar)
    rows = []
    for i in range(20):
        end = at(DAY, "09:00") + (i + 1) * MINUTE
        delta = 2 if end == at(DAY, "09:16") else 3 if end >= at(DAY, "09:17") else 0
        close = 100 + sign * delta
        rows.append({"close": close, "open": close, "low": close-.1, "high": close+.1,
                     "ma10": 98 if sign > 0 else 102, "ma20": 97 if sign > 0 else 103,
                     "previous_atr": 1})
    frame = pd.DataFrame(rows, index=[at(DAY, "09:00") + (i+1)*MINUTE for i in range(20)], dtype=float)
    features = SimpleNamespace(past=lambda key, end, n: frame.loc[:end].tail(n))
    def baseline(bar, candidate, state=True, cutoff=True):
        flags = {k: True for k in ("warmup_higher", "current_session_15m", "trend_15m", "trend_5m",
                                   "slope_5m_ready", "slope_5m_minimum", "slope_5m_maximum")}
        flags.update(state=state, entry_time=cutoff, efficiency=False, trend_activity=False,
                     trend_displacement=False, slope_1m_ready=False, slope_1m_minimum=False,
                     slope_1m_maximum=False, extension=True)
        flags.update(candidate=True,warmup_1m=True,trend_1m=True,vwap=True,oi=True,shock=True)
        return {"filters": flags, "snapshot": {}, "exit_flags": [], "pullback": None,
                "all_pass": False, "rejections": []}
    logic = ConfirmedLogic(SimpleNamespace(data=data, features=features), baseline, [])
    def higher(bar, candidate, base):
        source = bar.end.replace(minute=bar.end.minute//5*5)
        return {"filters": {"test_quality": True}, "source_5m_end": source.isoformat(),
                "source_15m_end": at(DAY, "09:15").isoformat()}
    logic.higher = higher
    candidate = {"direction": "LONG" if sign > 0 else "SHORT"}
    def evaluate(clock):
        end = at(DAY, clock)
        r = frame.loc[end]
        bar = replace(market.by_day[(DAY, KEY)][end-MINUTE], close=r.close, high=r.high, low=r.low)
        return logic.evaluate(bar, candidate)["_confirmed_channels"]["confirmed"]
    return logic, frame, evaluate


@pytest.mark.parametrize("sign", [1, -1])
def test_breakout_requires_setup_and_next_completed_continuation(market, sign):
    logic, _, evaluate = pattern(market, sign)
    assert not evaluate("09:15")["all_pass"]
    setup = evaluate("09:16")
    assert not setup["all_pass"] and setup["snapshot"]["price_confirmation"]["setup"]["kind"] == "confirmed_breakout"
    confirmed = evaluate("09:17")
    assert confirmed["all_pass"] and confirmed["pullback"]["kind"] == "confirmed_breakout"
    assert not evaluate("09:18")["all_pass"]
    assert len(logic.journal) >= 2


@pytest.mark.parametrize("case", ["no_continuation", "gap", "expiry", "lost_quality"])
def test_failed_stale_or_nonadjacent_confirmation_is_rejected(market, case):
    logic, frame, evaluate = pattern(market)
    evaluate("09:15")
    evaluate("09:16")
    if case == "no_continuation":
        frame.loc[at(DAY, "09:17"), "close"] = 102
    if case == "lost_quality":
        before = logic.higher
        logic.higher = lambda *a: before(*a) | {"filters": {"test_quality": False}}
    clock = "09:18" if case == "gap" else "09:20" if case == "expiry" else "09:17"
    assert not evaluate(clock)["all_pass"]


def test_pullback_touch_candle_must_start_after_arming(market):
    logic, frame, evaluate = pattern(market)
    # A touch ending at qualification time started before qualification.
    frame.loc[at(DAY, "09:15"), "ma10"] = 99.9
    frame.loc[at(DAY, "09:16"), "close"] = 100.2
    frame.loc[at(DAY, "09:16"), "high"] = 100.3
    frame.loc[at(DAY, "09:16"), "low"] = 100.
    frame.loc[at(DAY, "09:14"), "high"] = 101
    evaluate("09:15")
    setup = evaluate("09:16")["snapshot"]["price_confirmation"]["setup"]
    assert setup is None
    # A separate price path has a higher older reference, so a valid MA10
    # return is distinguished from a two-bar breakout.
    logic.windows.clear()
    logic.setups.clear()
    frame.loc[at(DAY, "09:15"), "high"] = 101.
    frame.loc[at(DAY, "09:16"), "ma10"] = frame.loc[at(DAY, "09:16"), "low"]
    evaluate("09:15")
    evaluate("09:16")
    frame.loc[at(DAY, "09:17"), "close"] = 100.5
    frame.loc[at(DAY, "09:17"), "high"] = 100.6
    setup = evaluate("09:17")["snapshot"]["price_confirmation"]["setup"]
    assert setup["kind"] == "confirmed_pullback" and setup["touch_event"] == at(DAY, "09:16").isoformat()
    frame.loc[at(DAY, "09:18"), "close"] = 100.7
    assert evaluate("09:18")["all_pass"]


def channel_row(legacy, confirmed, cost=True):
    base = {"filters": {"test": legacy}, "all_pass": legacy, "snapshot": {},
            "pullback": None, "exit_flags": [], "rejections": []}
    extra = {"filters": {"test": confirmed}, "all_pass": confirmed, "snapshot": {},
             "pullback": {"event": "SETUP", "kind": "confirmed_breakout"}, "exit_flags": [], "rejections": []}
    return copy.deepcopy(base) | {"filters": {"test": legacy, "cost": cost, "stop_reentry": True},
                                 "_confirmed_channels": {"legacy": base, "confirmed": extra},
                                 "_confirmation_time": at(DAY, "09:17").isoformat()}


def test_legacy_edge_keeps_priority_and_does_not_consume_other_pattern(market):
    e = engine(market)
    e.cfg["strategy"]["entry_confirmation"] = ENTRY
    row = channel_row(True, True)
    assert e.entry_trigger(KEY, row) and row["snapshot"]["entry_channel"] == "legacy"
    assert e.state(KEY).consumed == set()
    row = channel_row(True, True)
    assert e.entry_trigger(KEY, row) and row["snapshot"]["entry_channel"] == "confirmed_breakout"
    e.state(KEY).consumed.add("SETUP")
    assert not e.entry_trigger(KEY, channel_row(True, True))


@pytest.mark.parametrize("blocked", ["cost", "stop_reentry", "state"])
def test_common_gates_cannot_be_bypassed(market, blocked):
    e = engine(market)
    e.cfg["strategy"]["entry_confirmation"] = ENTRY
    row = channel_row(True, True)
    if blocked == "state":
        for channel in row["_confirmed_channels"].values():
            channel["filters"]["state"] = False
    else:
        row["filters"][blocked] = False
    assert not e.entry_trigger(KEY, row) and not row["all_pass"]


def test_only_three_adjacent_completed_same_session_bars_define_structure(market):
    features = Features(market)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:16")]
    result = structure_context(market, features, bar, "LONG")
    assert result["ready"] and len(result["sources"]) == 3
    assert result["extreme"] == min(r["low"] for r in result["sources"])
    assert result["source_end"] == at(DAY, "09:15").isoformat()
    # A later candle cannot alter a frozen snapshot.
    frame = features.frames[(KEY, 5)]
    frame.loc[at(DAY, "09:20"), "low"] = -1000
    assert structure_context(market, features, bar, "LONG") == result
    assert not structure_context(market, features, market.by_day[(DAY, KEY)][at(DAY, "10:31")], "LONG")["ready"]
    features.frames[(KEY, 5)] = frame.drop(at(DAY, "09:10"))
    assert not structure_context(market, features, bar, "LONG")["ready"]


def risk_signal(extreme=96, direction="LONG"):
    return {"contract": KEY, "time": at(DAY, "09:15").isoformat(), "direction": direction,
            "snapshot": {"atr_previous": 1, "structure_stop": {"ready": True,
                         "extreme": extreme, "atr_previous": 10, "sources": [], "source_end": "SOURCE"}}}


@pytest.mark.parametrize("direction,extreme,price", [("LONG", 96, 100), ("SHORT", 104, 100)])
def test_structure_widens_stop_and_keeps_original_floors_and_risk_budget(market, direction, extreme, price):
    e = engine(market)
    e.cfg["strategy"].update(structure_protection=STOP, protection_scale={"atr_multiple": 1., "roundtrip_cost_multiple": 1.})
    e.cfg["risk"]["max_lots_per_contract"] = 1000
    meta = market.metadata.get(KEY, DAY)
    row = risk_signal(extreme, direction)
    original = PortfolioBacktest.entry_protection(e, row, meta, 3, price=price)
    result = e.entry_protection(row, meta, 3, price=price)
    assert result["accepted"] and result["stop_loss_ticks"] == 401
    q0, *_ = e.allocator.allocate(meta, price, DAY, {}, e.cash, stop_loss_ticks=original["stop_loss_ticks"])
    q1, risk, _, _ = e.allocator.allocate(meta, price, DAY, {}, e.cash, stop_loss_ticks=result["stop_loss_ticks"])
    assert 0 < q1 < q0 and risk <= e.cash * e.cfg["risk"]["trade_risk_fraction"]
    fill = e.entry_protection(row, meta, 3, result["stop_loss_ticks"], price=price-1 if direction=="LONG" else price+1)
    assert fill["stop_loss_ticks"] == result["stop_loss_ticks"]


@pytest.mark.parametrize("case,expected", [("broken", "structure_already_broken"),
                                         ("gap", "structure_distance_exceeds_atr_cap"),
                                         ("missing", "structure_history_missing")])
def test_invalid_structure_and_fill_gap_cancel_entry(market, case, expected):
    e = engine(market)
    e.cfg["strategy"].update(structure_protection=STOP, protection_scale={"atr_multiple": 1., "roundtrip_cost_multiple": 1.})
    row = risk_signal()
    if case == "missing":
        row["snapshot"]["structure_stop"]["ready"] = False
    result = e.entry_protection(row, market.metadata.get(KEY, DAY), 3, price=95 if case=="broken" else 120 if case=="gap" else 100)
    assert not result["accepted"] and result["rejections"] == [expected]


def test_new_rules_require_offline_diagnostic_execution(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"].update(structure_protection=STOP, protection_scale={"atr_multiple": 1., "roundtrip_cost_multiple": 1.})
    cfg["execution"] = {"mode": "formal"}
    with pytest.raises(ResearchError, match="离线诊断"):
        validate_config(cfg)


@pytest.mark.parametrize("opening,reason", [(95.,"structure_already_broken"), (120.,"structure_distance_exceeds_atr_cap")])
def test_fill_hook_rechecks_frozen_structure_before_opening_position(market, opening, reason):
    e = engine(market)
    e.cfg["strategy"].update(structure_protection=STOP, protection_scale={"atr_multiple":1.,"roundtrip_cost_multiple":1.})
    row = risk_signal() | {"rank":1,"pullback":None,"filled":False}
    meta = market.metadata.get(KEY,DAY)
    e.admit_opportunity(row,meta,100,DAY,at(DAY,"09:14"))
    assert e.state(KEY).name == "ENTRY_PENDING"
    bar = replace(market.by_day[(DAY,KEY)][at(DAY,"09:15")],open=opening)
    e.fill_open(KEY,bar,bar.datetime)
    assert e.state(KEY).position is None and e.state(KEY).pending is None
    assert row["fill_structure_rejections"] == [reason] and not row["filled"]
    assert e.events[-1]["reason"] == "fill_structure_recheck"


def test_adverse_fill_reduces_quantity_instead_of_increasing_trade_risk(market):
    e = engine(market)
    e.cfg["strategy"].update(structure_protection=STOP,protection_scale={"atr_multiple":1.,"roundtrip_cost_multiple":1.})
    e.cfg["risk"]["max_lots_per_contract"] = 1000
    row = risk_signal() | {"rank":1,"pullback":None,"filled":False}
    meta = market.metadata.get(KEY,DAY)
    e.admit_opportunity(row,meta,100,DAY,at(DAY,"09:14"))
    reserved = e.state(KEY).pending["quantity"]
    bar = replace(market.by_day[(DAY,KEY)][at(DAY,"09:15")],open=102)
    e.fill_open(KEY,bar,bar.datetime)
    assert row["filled"] and e.state(KEY).position.quantity < reserved
    allocation = row["entry_allocation"]
    assert allocation["planned_risk"] <= allocation["single_trade_budget"]
    assert e.state(KEY).position.stop <= row["entry_protection"]["structure_anchor"]


def test_expanded_stop_respects_minimum_open_lots(market):
    e = engine(market)
    e.cfg["risk"]["initial_capital"] = 50000
    e.cfg["risk"]["max_lots_per_contract"] = 1000
    e.cfg["strategy"].update(structure_protection=STOP,protection_scale={"atr_multiple":1.,"roundtrip_cost_multiple":1.})
    meta = dict(market.metadata.get(KEY,DAY),min_open_lots=3)
    result = e.entry_protection(risk_signal(),meta,3,price=100)
    qty, _, _, reasons = e.allocator.allocate(meta,100,DAY,{},50000,stop_loss_ticks=result["stop_loss_ticks"])
    assert qty == 0 and reasons == ["minimum_open_lots"]


def test_confirmed_logic_wires_to_exported_stream_after_prepared_entry_bind(market,tmp_path):
    from research.coverage_audit import rows
    from research.experiments import run_one

    class LoggedLogic(ConfirmedLogic):
        def evaluate(self,*args,**kwargs):
            self.journal.append({"trigger":False,"time":args[0].end.isoformat(),"contract":args[0].key})
            return self.baseline(*args,**kwargs)

    class Prepared:
        def bind(self,original):
            self.trend = LoggedLogic(original,original.evaluate,[])
            return self
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def evaluate(self,*args,**kwargs):
            return self.trend.evaluate(*args,**kwargs)
        def finish(self):
            pass

    cfg = copy.deepcopy(market.cfg)
    cfg.setdefault("storage",{}).update(stream_signals=True,compress_signal_journal=True,compact_results=True)
    path,result = run_one(market,cfg,tmp_path/"runs",cfg["splits"]["validation"],
                          prepared_features=Features(market),prepared_entries=Prepared(),engine_factory=StructureBacktest)
    assert result["status"] == "completed"
    assert len(list(rows(path/"confirmation_setups.csv.gz"))) == len(result["signals"])
    for name in ("signals",*StructureBacktest.extra_journals):
        result[name].discard()


def test_signal_funnel_counts_the_new_confirmation_gates_and_rejections(market):
    from research.reporting import funnel

    _, _, evaluate = pattern(market)
    failed = evaluate("09:15")
    evaluate("09:16")
    passed = evaluate("09:17")
    records = [row | {"trigger":trigger,"risk_pass":trigger,"filled":trigger,"risk_rejections":[]}
               for row,trigger in [(failed,False),(passed,True)]]
    result = funnel(records)
    assert result["sequential"]["smooth"] == result["sequential"]["actual_fill"] == 1
    assert result["independent_rejections"]["price_pattern_confirmed"] == 1
    assert "trend_window_valid" not in result["independent_rejections"]


@pytest.mark.parametrize("sign", [1, -1])
def test_late_setup_survives_completed_five_minute_boundary(market, sign):
    logic, frame, evaluate = pattern(market, sign)
    logic.data.cfg["strategy"]["entry_confirmation"] = LIFETIME.copy()
    for clock, move in [("09:17", 0), ("09:18", 0), ("09:19", 2), ("09:20", 3)]:
        price = 100 + sign*move
        frame.loc[at(DAY, clock), ["close", "high", "low"]] = [price, price+.1, price-.1]
    evaluate("09:18")
    detail = evaluate("09:19")["snapshot"]["price_confirmation"]
    assert detail["setup"]["expires_at"] == at(DAY, "09:24").isoformat()
    result = evaluate("09:20")
    assert result["all_pass"]
    detail = result["snapshot"]["price_confirmation"]
    assert detail["confirmation"]["setup_end"] == at(DAY, "09:19").isoformat()
    assert detail["window"]["armed_at"] == at(DAY, "09:18").isoformat()
    assert [r["action"] for r in logic.events] == ["formed", "confirmed"]


@pytest.mark.parametrize("case,reason", [("expired", "expired"), ("broken", "structure_broken"),
    ("lost_quality", "higher_invalid"), ("gap", "nonadjacent")])
def test_lifetime_reports_the_terminal_cause(market, case, reason):
    logic, frame, evaluate = pattern(market)
    logic.data.cfg["strategy"]["entry_confirmation"] = LIFETIME.copy()
    if case == "expired":
        logic.data.cfg["strategy"]["entry_confirmation"]["valid_minutes"] = 1
    evaluate("09:15")
    evaluate("09:16")
    if case == "broken":
        frame.loc[at(DAY,"09:17"), "low"] = 99
    if case == "lost_quality":
        before = logic.higher
        logic.higher = lambda *args: before(*args) | {"filters": {"quality": False}}
    evaluate("09:18" if case == "gap" else "09:17")
    assert any(r["action"] == "cancelled" and r["reason"] == reason for r in logic.events)


def test_pattern_can_confirm_later_within_its_lifetime_if_structure_holds(market):
    logic, frame, evaluate = pattern(market)
    logic.data.cfg["strategy"]["entry_confirmation"] = LIFETIME.copy()
    evaluate("09:15")
    evaluate("09:16")
    frame.loc[at(DAY,"09:17"), ["close","high","low"]] = [102,102.1,101.9]
    assert not evaluate("09:17")["all_pass"]
    assert len(logic.setups) == 1
    result = evaluate("09:18")
    assert result["all_pass"] and result["pullback"]["event"] == at(DAY,"09:16").isoformat()
    assert [row["action"] for row in logic.events] == ["formed","observed","confirmed"]


def test_configured_lookback_and_lifetime_affect_actual_pattern(market):
    logic, frame, evaluate = pattern(market)
    logic.data.cfg["strategy"]["entry_confirmation"] = LIFETIME | {"valid_minutes": 7, "breakout_lookback_bars": 3}
    frame.loc[at(DAY,"09:13"), "high"] = 104
    evaluate("09:15")
    assert evaluate("09:16")["snapshot"]["price_confirmation"]["setup"] is None
    frame.loc[at(DAY,"09:13"), "high"] = 100.1
    logic.windows.clear()
    evaluate("09:15")
    setup = evaluate("09:16")["snapshot"]["price_confirmation"]["setup"]
    assert len(setup["reference_ends"]) == 3
    assert setup["expires_at"] == at(DAY,"09:23").isoformat()


@pytest.mark.parametrize("reference", ["ma10", "ma20"])
def test_pullback_recovery_confirms_without_another_expansion(market, reference):
    logic, frame, evaluate = pattern(market)
    logic.data.cfg["strategy"]["entry_confirmation"] = LIFETIME | {
        "patterns": ["pullback"], "pullback_reference": reference, "pullback_confirmation": "recovery_close"}
    frame.loc[at(DAY,"09:15"), "high"] = 101
    frame.loc[at(DAY,"09:16"), ["close", "high", "low", reference]] = [100.2,100.3,100.,100.]
    frame.loc[at(DAY,"09:17"), ["close", "high", "low"]] = [100.5,100.6,100.4]
    evaluate("09:15")
    evaluate("09:16")
    result = evaluate("09:17")
    assert result["all_pass"] and result["pullback"]["references"] == [int(reference[2:])]
    assert result["pullback"]["event"] == at(DAY,"09:16").isoformat()
    assert not logic.setups


def test_structure_lookback_is_read_from_config(market):
    cfg = copy.deepcopy(market.cfg)
    cfg["strategy"]["structure_protection"] = STOP | {"lookback_bars": 2}
    data = SimpleNamespace(cfg=cfg, metadata=market.metadata, calendar=market.calendar)
    bar = market.by_day[(DAY,KEY)][at(DAY,"09:11")]
    result = structure_context(data, Features(market), bar, "LONG")
    assert result["ready"] and len(result["sources"]) == 2


@pytest.mark.parametrize("direction,extreme", [("LONG",96), ("SHORT",104)])
def test_stop_widening_does_not_move_independent_profit_thresholds(market, direction, extreme):
    from research.execution import Position

    e = engine(market)
    e.cfg["strategy"].update(structure_protection=STOP, profit_protection=PROFIT,
                            protection_scale={"atr_multiple":1., "roundtrip_cost_multiple":1.})
    row = risk_signal(extreme, direction)
    meta = market.metadata.get(KEY,DAY)
    reference = PortfolioBacktest.entry_protection(e,row,meta,3,price=100)
    plan = e.entry_protection(row,meta,3,price=100)
    assert plan["stop_loss_ticks"] > reference["stop_loss_ticks"]
    assert plan["take_profit_ticks"] == reference["take_profit_ticks"]
    filled = e.entry_protection(row,meta,6,plan["stop_loss_ticks"],price=102 if direction=="LONG" else 98)
    assert filled["profit_protection_reference"] == plan["profit_protection_reference"]
    sign = 1 if direction == "LONG" else -1
    row["entry_protection"] = filled
    position = Position(KEY,meta,sign,1,100,100,at(DAY,"09:16"),DAY,
                        100-sign*filled["stop_loss_ticks"]*meta["tick_size"],
                        100+sign*filled["take_profit_ticks"]*meta["tick_size"],3,100,100,row)
    trailing = initial_trailing(position,{"atr_multiple":2.},{"activation_r":1.,"include_costs":True},1)
    assert abs(trailing["breakeven_activation_price"]-100) == reference["stop_loss_ticks"]*meta["tick_size"]


def test_diagnostics_separate_cost_timescales_and_account_capacity(market):
    from research.signals import SignalLogic

    e = engine(market)
    e.cfg["strategy"].update(protection_scale={"atr_multiple":1.,"roundtrip_cost_multiple":2.})
    bar = market.by_day[(DAY,KEY)][at(DAY,"09:16")]
    candidate = {"direction":"LONG","selected":True,"rank":1}
    evaluation = SignalLogic(e.data,e.features).evaluate(bar,candidate)
    evaluation["filters"] = {k:True for k in evaluation["filters"]} | {"cost":False}
    e.diagnose_opportunity(bar,candidate,evaluation)
    row = e.opportunity_diagnostics[-1]
    assert row["cost_to_atr_1m"] > 0 and row["cost_to_atr_5m"] > 0
    required = row["minimum_required_capital_and_equity"]
    qty, *_ = e.allocator.allocate(market.metadata.get(KEY,DAY),bar.close,DAY,{},required+1,
        stop_loss_ticks=round(row["initial_risk_distance"]/market.metadata.get(KEY,DAY)["tick_size"]))
    assert qty >= row["minimum_open_lots"]
    assert row["cost_pass"] is False


def test_new_research_budget_allows_normal_drawdown_in_previously_empty_window():
    from research.structure_assessment import research_checks

    criteria = {"max_window_drawdown_cny":10000,"max_window_loss_cny":10000,
                "minimum_trades_for_further_review":20}
    checks = research_checks([{"max_drawdown":1500,"net_profit":-500,"trade_count":3}],criteria)
    assert checks["drawdown_within_budget"] and checks["loss_within_budget"]
    assert not checks["enough_trades_for_further_review"]


@pytest.mark.parametrize("field,value", [("valid_minutes",7),("breakout_lookback_bars",3),("pullback_reference","ma20")])
def test_legacy_configuration_rejects_apparent_tuning_but_new_recipe_accepts_it(market,field,value):
    cfg = copy.deepcopy(market.cfg)
    cfg["execution"] = {"mode":"diagnostic"}
    cfg["strategy"].update(entry_confirmation=ENTRY | {field:value},
        slope_band={"min_move_ticks":1.,"timeframes":{label:{"lookback_bars":3,"min_atr_per_bar":.01,"max_atr_per_bar":.3}
                   for label in ("1m","5m")}},trend_quality={"min_price_changes":3,"min_displacement_atr":1.,"min_displacement_ticks":2},
        entry_cost_filter={"max_cost_atr":.5})
    cfg["strategy"]["entry_mode"] = "direct"
    cfg.pop("baseline_expectation",None)
    with pytest.raises(ResearchError,match="旧确认方案参数固定"):
        validate_config(cfg)
    cfg["strategy"]["entry_confirmation"] = LIFETIME | {field:value}
    validate_config(cfg)


def test_september_batch_preserves_verified_training_proof_for_each_configuration(market, tmp_path, monkeypatch):
    import json

    from research import prepared_review, structure_audit, structure_followup
    from research.experiments import scope_data
    from research.prepared_review import PreparedDataset

    cutoff = market.cfg["splits"]["train"]["end"]
    full_training_hash = market.training_fingerprint(cutoff)
    variants = ("control", "lifetime", "stop_only")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "window": {"end": market.cfg["splits"]["validation"]["end"]}}))

    def configuration(month, variant, plan_path):
        cfg = copy.deepcopy(market.cfg)
        cfg["optimization_review"] = {"month": month, "variant": variant}
        return {"order": variants}, tmp_path, cfg

    def prepare(source, cfg):
        # Full training was checked before retaining only the replay rows.
        data = PreparedDataset([], cfg)
        data.verified_training_cutoff = cutoff
        data.verified_training_fingerprint = full_training_hash
        return data, SimpleNamespace(), {}

    completed = []

    def replay(month, variant, plan_path, prepared=None):
        _, _, cfg = configuration(month, variant, plan_path)
        data, _, _ = prepared if prepared is not None else prepare(None, cfg)
        scoped, _ = scope_data(data, cfg, "shared")
        assert scoped.training_fingerprint(cutoff) == full_training_hash
        completed.append(variant)
        return {"directory": str(tmp_path)}

    monkeypatch.setattr(structure_followup, "configuration", configuration)
    monkeypatch.setattr(structure_followup, "run", replay)
    monkeypatch.setattr(prepared_review, "prepare_review", prepare)
    monkeypatch.setattr(structure_audit, "audit", lambda *args: None)
    structure_followup.batch("2026-09", tmp_path / "plan.json")
    assert completed == list(variants)
