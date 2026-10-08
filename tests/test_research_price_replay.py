"""SYNTHETIC_TEST_ONLY fixtures verify causality and price/state semantics."""

import copy
import json
from dataclasses import replace

import pytest

from research.__main__ import main
from research.calendar import MINUTE, at, stamp
from research.config import ResearchError, digest, read_config, validate_config
from research.data import Dataset, load_data
from research.diagnostic import contract_specifications
from research.execution import PortfolioBacktest, protective_touch
from research.experiments import (
    calibrate_ticks,
    freeze_run,
    run_one,
    sweep,
    walk_forward,
)
from research.fixtures import create_fixture
from research.price_replay import (
    PriceReplayParameters,
    RulePriceReplay,
    prepare_price_replay,
)
from research.reporting import metrics, report_run

DAY, KEY = "2026-01-08", "aa2603.SHFE"


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    cfg = create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY_price_paths"))
    cfg["strategy"].update(k=2, entry_mode="direct")
    cfg["baseline_expectation"] = {"schema": 1, "strategy": {"k": 2, "entry_mode": "direct"}}
    # Fees/margin are absent. Price replay must never invent them to open positions.
    for meta in cfg["metadata"]["contracts"]:
        meta.update(margin_rate=None, fees=[], verified=False)
    return load_data(cfg)


def prepared(data, tmp_path, missing=None):
    cfg = copy.deepcopy(data.cfg)
    cfg["execution"] = {"mode": "formal"}
    cfg.pop("price_replay", None)
    cfg.pop("calibration_snapshot", None)
    cal = calibrate_ticks(data.until(cfg["splits"]["train"]["end"]), cfg)
    for product in missing or []:
        cal["products"].pop(product, None)
        cal["missing_products"][product] = {"reasons": ["no_positive_volume_training_minutes"]}
    source, calibration = tmp_path / "config.json", tmp_path / "calibration.json"
    source.write_text(json.dumps(cfg))
    calibration.write_text(json.dumps(cal))
    result = prepare_price_replay(source, calibration, tmp_path / "prepared")
    return read_config(result["config"])


def test_correct_k_propagates_and_runtime_disagrees_with_wrong_k(market, tmp_path):
    cfg = prepared(market, tmp_path)
    directory, result = run_one(market, cfg, tmp_path / "runs", cfg["splits"]["validation"], make_report=True, price_replay=True)
    assert result["status"] == "completed"
    for name in ("config_snapshot.json", "manifest.json"):
        saved = json.loads((directory / name).read_text())
        saved = saved["configuration"] if name == "manifest.json" else saved
        assert saved["strategy"]["k"] == 2 and saved["strategy"]["entry_mode"] == "direct"
    assert json.loads((directory / "manifest.json").read_text())["baseline_actual"] == {"k": 2, "entry_mode": "direct"}
    altered = copy.deepcopy(cfg)
    altered["strategy"]["k"] = 1
    with pytest.raises(ResearchError, match="固定基准"):
        validate_config(altered)
    with pytest.raises(ResearchError, match="固定基准"):
        RulePriceReplay(market, altered)
    assert "K=2" in (directory / "report.md").read_text()


def test_paths_run_with_unknown_costs_but_no_account_metrics(market, tmp_path):
    cfg = prepared(market, tmp_path)
    result = RulePriceReplay(market, cfg).run(DAY, DAY)
    assert result["trades"] and not result["open_positions"]
    assert "metrics" not in result and "equity" not in result
    for trade in result["trades"]:
        assert trade["hypothetical"] and trade["ticks_pnl"] is None
        assert trade["cost_status"] == "NOT_MODELED_UNKNOWN_COSTS_NOT_ZERO"
        assert not {"net_pnl", "gross_pnl", "fees", "entry_fee", "margin", "cash"} & trade.keys()
        assert trade["price_points"] == pytest.approx((1 if trade["direction"] == "LONG" else -1) * (trade["exit_price"] - trade["entry_price"]))
        assert trade["r_multiple"] == pytest.approx(trade["price_points"] / trade["planned_stop_distance"])
    with pytest.raises(ResearchError, match="禁止计算账户"):
        metrics(result, 1_000_000)
    formal = copy.deepcopy(cfg)
    formal.pop("price_replay")
    with pytest.raises(ResearchError, match="正式回测配置缺失"):
        PortfolioBacktest(market, formal).run(DAY, DAY)


def test_repeated_triggers_respect_single_position_and_cooldown(market, tmp_path):
    cfg = prepared(market, tmp_path)
    result = RulePriceReplay(market, cfg).run(DAY, DAY)
    assert result["trades"]
    for key in {t["contract"] for t in result["trades"]}:
        paths = sorted([t for t in result["trades"] if t["contract"] == key], key=lambda t: t["entry_time"])
        for a, b in zip(paths, paths[1:], strict=False):
            assert stamp(b["entry_time"]) >= stamp(a["exit_time"]) + cfg["strategy"]["cooldown_minutes"] * MINUTE
    assert sum(r["hypothetical_filled"] for r in result["signals"]) == len(result["trades"])
    assert all(t["entry_signal_time"] <= t["entry_time"] for t in result["trades"])


def pending(engine, time=None):
    time = time or at(DAY, "09:20")
    engine.state(KEY).name = "ENTRY_PENDING"
    engine.state(KEY).pending = {"signal": {"time": time.isoformat(), "direction": "LONG", "snapshot": {}, "rank": 1, "r8": .01, "pullback": None}}


def test_zero_volume_then_missing_minute_waits_for_actual_next_open(market, tmp_path):
    engine = RulePriceReplay(market, prepared(market, tmp_path))
    pending(engine)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:20")]
    engine.fill_open(KEY, replace(bar, volume=0), bar.datetime)
    engine.fill_open(KEY, None, bar.datetime + MINUTE)
    assert engine.state(KEY).name == "ENTRY_PENDING" and not engine.state(KEY).position
    later = replace(bar, datetime=bar.datetime + 2 * MINUTE, open=150, high=150.3, low=149.7, close=150)
    engine.fill_open(KEY, later, later.datetime)
    pos = engine.state(KEY).position
    assert pos.opened == later.datetime and pos.raw_price == 150
    assert pos.price == pytest.approx(150 + engine.cfg["strategy"]["slippage_ticks"] * pos.meta["tick_size"])


@pytest.mark.parametrize("locked", [False, True])
def test_nontradable_or_known_locked_bar_never_fills(market, tmp_path, locked):
    engine = RulePriceReplay(market, prepared(market, tmp_path))
    pending(engine)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:20")]
    bar = replace(bar, open=120, high=120, low=120, close=120, limit_up=120) if locked else replace(bar, tradable=False)
    engine.fill_open(KEY, bar, bar.datetime)
    assert engine.state(KEY).name == "ENTRY_PENDING" and not engine.state(KEY).position


def test_dual_touch_uses_stop_first_and_gap_r_can_exceed_one(market, tmp_path):
    engine = RulePriceReplay(market, prepared(market, tmp_path))
    pending(engine)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:20")]
    engine.fill_open(KEY, bar, bar.datetime)
    pos = engine.state(KEY).position
    touched = replace(bar, open=pos.price, high=pos.target + 1, low=pos.stop - 1)
    hit = protective_touch(pos, touched)
    assert hit["reason"] == "fixed_stop" and hit["ambiguous"]
    engine.close(KEY, touched, hit["raw_price"], touched.end, hit["flags"], hit["ambiguous"], hit["gap"])
    assert engine.trades[-1]["exit_reason"] == "fixed_stop" and engine.trades[-1]["r_multiple"] < -1
    pending(engine, at(DAY, "10:00"))
    engine.fill_open(KEY, bar, at(DAY, "10:00"))
    pos = engine.state(KEY).position
    gap = replace(bar, open=pos.stop - 2, low=pos.stop - 3, high=pos.stop - 1)
    hit = protective_touch(pos, gap)
    assert hit["gap"] and hit["raw_price"] == gap.open
    engine.close(KEY, gap, hit["raw_price"], at(DAY, "10:01"), hit["flags"], gap=hit["gap"])
    assert engine.trades[-1]["r_multiple"] < -1


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("reason", ["fixed_stop", "fixed_target"])
def test_replay_opening_gap_reason_precedes_later_touches(
    market, tmp_path, direction, reason
):
    engine = RulePriceReplay(market, prepared(market, tmp_path))
    pending(engine)
    engine.state(KEY).pending["signal"]["direction"] = direction
    base = market.by_day[(DAY, KEY)][at(DAY, "09:20")]
    engine.fill_open(KEY, base, base.datetime)
    position = engine.state(KEY).position
    sign = position.sign
    opening = position.stop - sign if reason == "fixed_stop" else position.target + sign
    bar = replace(
        base, datetime=base.datetime + MINUTE, open=opening, close=opening,
        high=max(position.stop, position.target, opening) + 1,
        low=min(position.stop, position.target, opening) - 1,
    )
    hit = protective_touch(position, bar)
    engine.close(
        KEY, bar, hit["raw_price"], bar.datetime,
        hit["flags"], hit["ambiguous"], hit["gap"],
    )
    trade = engine.trades[-1]
    assert trade["exit_reason"] == reason and trade["exit_flags"] == [reason]
    assert trade["exit_time"] == bar.datetime.isoformat()
    assert trade["exit_price"] == pytest.approx(opening - sign * 0.01)
    assert (trade["r_multiple"] > 0) == (reason == "fixed_target")
    assert trade["gap"] and not trade["ambiguous_bar"] and engine.ambiguities == 0


def test_unfilled_exit_keeps_position_and_end_risk(market, tmp_path):
    engine = RulePriceReplay(market, prepared(market, tmp_path))
    pending(engine)
    bar = market.by_day[(DAY, KEY)][at(DAY, "09:20")]
    engine.fill_open(KEY, bar, bar.datetime)
    engine.request_exit(KEY, at(DAY, "14:50"), ["time_force"])
    engine.fill_open(KEY, None, at(DAY, "15:00"))
    assert engine.state(KEY).position is not None and engine.state(KEY).name == "EXIT_PENDING"
    assert not engine.trades


def test_future_prices_do_not_change_prior_paths(market, tmp_path):
    cfg = prepared(market, tmp_path)
    cutoff = at(DAY, "11:00")
    before = RulePriceReplay(market, cfg).run(DAY, DAY)
    altered = Dataset([replace(b, open=b.open + 20, close=b.close + 20, high=b.high + 20, low=b.low + 20, open_interest=b.open_interest + 500) if b.datetime >= cutoff else b for b in market.bars], market.cfg, market.quality)
    after = RulePriceReplay(altered, cfg).run(DAY, DAY)
    assert before["daily_candidates"] == after["daily_candidates"]
    for name, clock in (("signals", "time"), ("events", "time"), ("trades", "exit_time")):
        assert [r for r in before[name] if r[clock] < cutoff.isoformat()] == [r for r in after[name] if r[clock] < cutoff.isoformat()]


def test_training_missing_rank_one_kept_without_backfill(market, tmp_path):
    cfg = copy.deepcopy(market.cfg)
    base = next(m for m in cfg["metadata"]["contracts"] if m["product"] == "aa")
    bars = list(market.bars)
    for product in ("ee", "ff"):
        cfg["metadata"]["contracts"].append({**base, "product": product, "symbol": product + "2603"})
        cfg["strategy"]["fixed_ticks"][product] = copy.deepcopy(cfg["strategy"]["fixed_ticks"]["aa"])
        bars.extend(replace(b, product=product, symbol=product + "2603") for b in market.bars if b.key == KEY)
    data = Dataset(bars, cfg, market.quality)
    replay = prepared(data, tmp_path, missing=["aa"])
    result = RulePriceReplay(data, replay).run(DAY, DAY)
    longs = [r for r in result["daily_candidates"] if r["group"] == "commodity" and r["direction"] == "LONG"]
    assert [(r["product"], r["rank"], r["selected"]) for r in longs] == [("aa", 1, True), ("ee", 2, True), ("ff", 3, False)]
    assert not any(o["contract"] in {KEY, "ff2603.SHFE"} for o in result["orders"])
    assert any(t["product"] == "ee" for t in result["trades"])


def test_replay_cannot_search_freeze_or_read_locked_test(market, tmp_path):
    cfg = prepared(market, tmp_path)
    with pytest.raises(ResearchError, match="锁定测试"):
        RulePriceReplay(market, cfg).run(DAY, "2026-01-09")
    with pytest.raises(ResearchError, match="选K"):
        sweep(market, cfg, tmp_path / "sweep")
    with pytest.raises(ResearchError, match="选参"):
        walk_forward(market, cfg, tmp_path / "walk")
    directory, result = run_one(market, cfg, tmp_path / "runs", cfg["splits"]["validation"], price_replay=True)
    with pytest.raises(ResearchError, match="不能冻结"):
        freeze_run(directory, tmp_path / "frozen.json")
    assert report_run(directory).exists() and result["account_metrics_calculated"] is False
    path = tmp_path / "prepared" / "price_replay_config.json"
    assert main(["price-replay", "--config", str(path), "--split", "test", "--synthetic"]) == 2
    assert main(["backtest", "--config", str(path), "--synthetic"]) == 2


def test_specification_after_training_but_before_trade_is_allowed():
    row = {"data_standard": "2026-09-13", "ContractSize": "5吨/手", "MinimumPriceFluctuation": "10元/吨", "PriceQuotation": "元/吨", "ContractMultiplier": ""}
    text = "<script>let pageList = " + json.dumps([row]) + "</script>"
    spec = contract_specifications(text, at("2026-09-14", "09:00"))
    assert spec["published_date"] > "2026-09-11" and spec["tick_size"] == 10
    with pytest.raises(ResearchError, match="决策前"):
        contract_specifications(text, at("2026-09-13", "09:00"))
    row["data_standard"] = ""
    with pytest.raises(ResearchError, match="无日期"):
        contract_specifications("<script>let pageList = " + json.dumps([row]) + "</script>", at("2026-09-14", "09:00"))


def test_changed_training_or_protection_cannot_reuse_replay(market, tmp_path):
    cfg = prepared(market, tmp_path)
    wrong = copy.deepcopy(cfg)
    wrong["strategy"]["fixed_ticks"]["aa"]["stop_loss_ticks"] += 1
    wrong["price_replay"]["strategy_hash"] = digest(wrong["strategy"])
    with pytest.raises(ResearchError, match="训练候选"):
        PriceReplayParameters(wrong, market.metadata)
    changed = Dataset([replace(b, volume=b.volume + 1) if b.trading_day == "2026-01-07" else b for b in market.bars], market.cfg, market.quality)
    with pytest.raises(ResearchError, match="训练数据指纹"):
        RulePriceReplay(changed, cfg).run(DAY, DAY)
