import copy
from dataclasses import replace

import pytest

from research.calendar import at, MINUTE
from research.config import ResearchError, validate_config
from research.data import Dataset, load_data
from research.execution import PortfolioBacktest, Position, fee, protective_touch, slipped
from research.fixtures import create_fixture
from research.optimization_rules import afternoon_candidates, cost_check
from research.trailing import advance_trailing, initial_trailing

DAY, KEY = "2026-01-08", "aa2603.SHFE"
TRAIL = {"activation": "original_target", "atr_multiple": 2.0}
BE = {"activation_r": 1.0, "include_costs": True}


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    return load_data(create_fixture(tmp_path_factory.mktemp("SYNTHETIC_OPTIMIZATION_TEST_ONLY")))


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("mode,amount", [("fixed", 1.0), ("rate", 0.001)])
def test_break_even_price_covers_actual_fees_and_exit_slippage_in_both_directions(market, sign, mode, amount):
    meta = dict(market.metadata.get(KEY, DAY), tick_size=0.25, value_per_price=10,
                fees=[{"effective_from": DAY, **{side: {"mode": mode, "value": amount} for side in ["open", "close_today", "close_yesterday"]}}])
    entry_fee = fee(meta, DAY, "open", 100, 3)
    p = Position(KEY, meta, sign, 3, 100, 100-sign*0.25, at(DAY,"09:15"), DAY, 100-sign*2, 100+sign*4, entry_fee, 100, 100, {})
    p.trailing = initial_trailing(p, TRAIL, BE, 1)
    b = replace(market.by_day[(DAY,KEY)][at(DAY,"09:15")], open=100, high=102.5 if sign>0 else 100.5,
                low=99.5 if sign>0 else 97.5, close=100+sign*1.5)
    assert protective_touch(p,b) is None
    diagnostic = advance_trailing(p,b,1)
    assert p.trailing["breakeven_active"] and not p.trailing["active"]
    assert p.trailing["known_at"] == b.end.isoformat()
    assert diagnostic["effective_from"] == "next_available_open"
    raw=p.trailing["breakeven_price"]
    exit_price=slipped(raw,-sign,meta,1)
    net=sign*(exit_price-p.price)*p.quantity*meta["value_per_price"]-entry_fee-fee(meta,DAY,"close_today",exit_price,p.quantity)
    assert net>=-1e-8
    next_bar=replace(b,datetime=at(DAY,"09:16"),open=raw-sign*0.5,high=raw+1,low=raw-1)
    hit=protective_touch(p,next_bar)
    assert hit["reason"]=="breakeven_stop" and hit["gap"]
    assert hit["raw_price"]==next_bar.open


def test_break_even_cannot_use_future_extremes_or_loosen_a_tighter_trailing_stop(market):
    meta=market.metadata.get(KEY,DAY)
    p=Position(KEY,meta,1,1,100,100,at(DAY,"09:15"),DAY,98,104,fee(meta,DAY,"open",100,1),100,100,{})
    p.trailing=initial_trailing(p,TRAIL,BE,1)
    b=replace(market.by_day[(DAY,KEY)][at(DAY,"09:15")],open=100,high=103,low=97,close=102)
    assert protective_touch(p,b)["reason"]=="fixed_stop"
    assert not p.trailing["breakeven_active"]
    b=replace(b,low=99,high=110,close=109)
    advance_trailing(p,b,0.5)
    prior=p.trailing["stop_price"]
    advance_trailing(p,replace(b,datetime=at(DAY,"09:16"),high=109,close=108),20)
    assert p.trailing["stop_price"]==prior


def test_cost_gate_can_become_eligible_while_the_original_signal_remains_true(market):
    cfg=copy.deepcopy(market.cfg)
    cfg["strategy"]["entry_cost_filter"]={"max_cost_atr":0.5}
    engine=PortfolioBacktest(Dataset(market.bars,cfg))
    def evaluate(bar,candidate,state_allows=True,before_cutoff=True):
        active=bar.key==KEY and at(DAY,"09:15")<=bar.end<=at(DAY,"09:16") and bar.trading_day==DAY
        meta=market.metadata.get(KEY,DAY)
        costs=fee(meta,DAY,"open",bar.close,1)+fee(meta,DAY,"close_today",bar.close,1)
        distance=costs/meta["value_per_price"]+2*cfg["strategy"]["slippage_ticks"]*meta["tick_size"]
        atr=distance/(0.6 if bar.end==at(DAY,"09:15") else 0.4)
        return {"filters":{"state":state_allows,"manual":active,"entry_time":before_cutoff},"all_pass":active and state_allows and before_cutoff,"rejections":[],"pullback":None,"snapshot":{"atr_previous":atr},"exit_flags":[]}
    engine.logic.evaluate=evaluate
    result=engine.run(DAY,DAY)
    assert len(result["trades"])==1
    assert result["trades"][0]["entry_signal_time"]==at(DAY,"09:16").isoformat()
    assert result["trades"][0]["entry_cost_check"]["accepted"]
    rejected=[s for s in result["signals"] if s["contract"]==KEY and s["time"]==at(DAY,"09:15").isoformat()][0]
    assert not rejected["filters"]["cost"] and not rejected["trigger"]


@pytest.mark.parametrize("atr", [None,0,float("nan")])
def test_cost_gate_rejects_missing_or_nonpositive_signal_atr(market,atr):
    strategy=market.cfg["strategy"]|{"entry_cost_filter":{"max_cost_atr":0.5}}
    assert not cost_check({"snapshot":{"atr_previous":atr}},market.metadata.get(KEY,DAY),strategy,1,100)["accepted"]


@pytest.mark.parametrize("key,rule",[("entry_cost_filter",{"max_cost_atr":True}),("breakeven",{"activation_r":1,"include_costs":False}),("afternoon_rerank",{"opening_minutes":7,"rank_by":"afternoon_return","replace":True})])
def test_unbounded_or_undeclared_optimization_rules_are_rejected(market,key,rule):
    cfg=copy.deepcopy(market.cfg);cfg["strategy"][key]=rule
    with pytest.raises(ResearchError):validate_config(cfg)


def test_afternoon_ranking_uses_only_eight_completed_minutes_and_reports_gaps(market):
    pool=[{"meta":m,"contract":m["symbol"]+'.'+m["exchange"],"group":m["group"],"previous_volume":1000}
          for m in market.cfg["metadata"]["contracts"]]
    opening=at(DAY,"13:30");cutoff=opening+8*MINUTE
    expected,_=afternoon_candidates(market,DAY,pool,opening,2,cutoff=cutoff)
    assert expected and all(r['ranking_time']==cutoff.isoformat() for r in expected)
    future=[replace(b,open=9999,high=9999,low=9999,close=9999) if b.datetime==cutoff else b for b in market.bars]
    actual,_=afternoon_candidates(Dataset(future,market.cfg),DAY,pool,opening,2,cutoff=cutoff)
    assert actual==expected
    with pytest.raises(ResearchError,match='完整'):
        afternoon_candidates(market,DAY,pool,opening,2,cutoff=cutoff-MINUTE)
    missing=[b for b in market.bars if not (b.key==KEY and b.datetime==opening+4*MINUTE)]
    rows,excluded=afternoon_candidates(Dataset(missing,market.cfg),DAY,pool,opening,2,cutoff=cutoff)
    assert not any(r['contract']==KEY for r in rows)
    assert any(r['contract']==KEY and 'missing' in r['reasons'][0] for r in excluded)
