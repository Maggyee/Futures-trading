"""SYNTHETIC_TEST_ONLY market fixtures; archived real parameters are arithmetic evidence."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from research.__main__ import main
from research.calendar import MINUTE, at
from research.config import ResearchError, digest
from research.cost_review import fee_reference
from research.data import Dataset, load_data
from research.diagnostic import contract_specifications, timing_check
from research.execution import PortfolioBacktest, Position, RiskAllocator, fee
from research.execution_parameters import ExecutionParameters
from research.experiments import (
    apply_calibration,
    calibrate_ticks,
    freeze_run,
    run_one,
    scope_data,
    sweep,
)
from research.feature_cache import read_frames, write_frames
from research.fixtures import create_fixture
from research.signals import Features
from research.storage import SpaceBudget

DAY = "2026-01-08"


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    cfg = create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY_diagnostic"))
    return load_data(cfg)


def profile(data, enabled=None, missing=None):
    cfg = copy.deepcopy(data.cfg)
    train = data.until(cfg["splits"]["train"]["end"])
    calibration = calibrate_ticks(train, cfg)
    for product, reasons in (missing or {}).items():
        calibration["products"].pop(product, None)
        calibration["missing_products"][product] = {"reasons": reasons}
    cfg = apply_calibration(cfg, calibration, 0)
    rules = []
    for meta in cfg["metadata"]["contracts"]:
        if enabled is not None and meta["product"] not in enabled:
            continue
        rules.append(
            {
                "contract": meta["symbol"] + "." + meta["exchange"],
                "trading_day": DAY,
                "source_date": "2026-01-07",
                "available_at": at("2026-01-07", "16:00").isoformat(),
                "margin_effective_at": at("2026-01-07", "15:00").isoformat(),
                "effective_from": data.calendar.bounds(DAY, meta)[0].isoformat(),
                "effective_to": (
                    data.calendar.bounds(DAY, meta)[1] + MINUTE
                ).isoformat(),
                **{
                    k: meta[k]
                    for k in (
                        "tick_size",
                        "value_per_price",
                        "margin_rate",
                        "listed",
                        "expiry",
                    )
                },
                "fees": {
                    k: meta["fees"][0][k]
                    for k in ("open", "close_today", "close_yesterday")
                },
                "sources": [{"label": "SYNTHETIC_TEST_ONLY"}],
            }
        )
    qualification = {
        "schema": 1,
        "kind": "DIAGNOSTIC_EXECUTION_ONLY",
        "training_window": cfg["splits"]["train"],
        "validation_window": cfg["splits"]["validation"],
        "calibration_hash": digest(calibration),
        "fixed_ticks_hash": digest(cfg["strategy"]["fixed_ticks"]),
        "training_ready_products": sorted(calibration["products"]),
        "training_missing_products": calibration["missing_products"],
        "allowed_exchanges": ["SHFE", "CFFEX"],
        "fee_model": "exchange_only",
        "rules": rules,
        "rule_rejections": {},
        "locked_test_read": False,
        "assumptions": ["SYNTHETIC_TEST_ONLY"],
        "ranking_policy": "保持原排名，不补选",
    }
    cfg["execution"] = {
        "mode": "diagnostic",
        "qualification": qualification,
        "qualification_hash": digest(qualification),
    }
    return cfg


def refresh(cfg):
    cfg["execution"]["qualification_hash"] = digest(cfg["execution"]["qualification"])


def test_omitting_unselected_observations_preserves_execution_and_selected_signals(market):
    # Duplicate a viable product with its own contract/product identity, creating
    # a true rank-2 candidate rather than relying on each fixture group's rank-1.
    viable = next(r for r in market.cfg["metadata"]["contracts"] if r["product"] == "aa")
    key = viable["symbol"] + "." + viable["exchange"]
    base = copy.deepcopy(market.cfg)
    base["metadata"]["contracts"].append({**viable, "symbol": "zz2605", "product": "zz"})
    expanded = Dataset(list(market.bars) + [replace(b, symbol="zz2605", product="zz") for b in market.by_contract[key]], base,
                       daily=list(market.daily) + [replace(d, symbol="zz2605", product="zz") for d in market.daily if d.key == key])
    full_cfg = profile(expanded)
    full_cfg["strategy"]["k"] = 1
    full = PortfolioBacktest(expanded, full_cfg).run(DAY, DAY)
    selected_cfg = copy.deepcopy(full_cfg)
    selected_cfg.setdefault("storage", {})["record_unselected_signals"] = False
    selected = PortfolioBacktest(expanded, selected_cfg).run(DAY, DAY)
    assert selected["unselected_signal_observations_skipped"] > 0
    for name in ("trades", "orders", "equity", "daily_pool", "daily_candidates", "candidate_execution", "events"):
        assert selected[name] == full[name]
    assert selected["signals"] == [r for r in full["signals"] if r["filters"]["candidate"]]
    assert len(selected["signals"]) + selected["unselected_signal_observations_skipped"] == len(full["signals"])


def test_schema_two_explicit_older_schedule_does_not_weaken_schema_one(market):
    cfg = profile(market)
    q = cfg["execution"]["qualification"]
    row = q["rules"][0]
    row.update(
        source_date="2026-01-06",
        source_basis="dated_schedule_continuity",
        available_at=at("2026-01-06", "16:00").isoformat(),
        margin_effective_at=at("2026-01-06", "15:00").isoformat(),
    )
    refresh(cfg)
    with pytest.raises(ResearchError):
        ExecutionParameters(cfg, market.metadata)
    q["schema"] = 2
    refresh(cfg)
    assert ExecutionParameters(cfg, market.metadata).resolve(
        row["contract"], at(DAY, "10:00")
    )[0]
    row["source_date"] = DAY
    refresh(cfg)
    with pytest.raises(ResearchError, match="当日收盘"):
        ExecutionParameters(cfg, market.metadata)


def test_supplier_specification_assumption_requires_explicit_diagnostic_flag(market):
    cfg = profile(market)
    q = cfg["execution"]["qualification"]
    q["schema"] = 2
    q["rules"][0]["specification_basis"] = "supplier_specification_continuity_assumed"
    refresh(cfg)
    with pytest.raises(ResearchError, match="延续假设"):
        ExecutionParameters(cfg, market.metadata)
    q["supplier_specification_continuity_assumed"] = True
    refresh(cfg)
    assert ExecutionParameters(cfg, market.metadata)


@pytest.mark.parametrize("remaining", [None, 1])
def test_minimum_open_order_never_inflates_risk_capacity(market, remaining):
    cfg = copy.deepcopy(market.cfg)
    cfg["risk"]["max_lots_per_contract"] = 1
    meta = {**market.metadata.get("aa2603.SHFE", DAY), "min_open_lots": 2}
    quantity, risk, margin, reasons = RiskAllocator(cfg).allocate(
        meta,
        100,
        DAY,
        {},
        cfg["risk"]["initial_capital"],
        remaining_open_lots=remaining,
    )
    assert (quantity, risk, margin) == (0, 0, 0)
    assert reasons == ["minimum_open_lots"]


def limited_engine(market):
    cfg = profile(market)
    cfg["execution"]["qualification"]["schema"] = 2
    for row in cfg["execution"]["qualification"]["rules"]:
        row.update(min_open_lots=2, daily_open_limit=3)
    refresh(cfg)
    return PortfolioBacktest(market, cfg)


def example_signal():
    return {
        "contract": "aa2603.SHFE",
        "direction": "LONG",
        "rank": 1,
        "pullback": None,
        "time": at(DAY, "09:20").isoformat(),
        "r8": 0.01,
        "snapshot": {},
    }


def test_daily_open_limit_counts_fills_across_closes_and_resets_by_day(market):
    engine = limited_engine(market)
    key = "aa2603.SHFE"
    meta, _ = engine.parameters.resolve(key, at(DAY, "09:20"))
    signal = example_signal()
    bar = engine.data.by_day[(DAY, key)][at(DAY, "09:21")]
    engine.admit_opportunity(signal, meta, bar.open, DAY, at(DAY, "09:20"))
    assert engine.state(key).pending["quantity"] == 3
    assert (
        engine.opened_lots[(DAY, key)] == 0
    )  # Pending orders do not consume executions.
    engine.fill_open(key, bar, bar.datetime)
    assert engine.opened_lots[(DAY, key)] == 3
    assert engine.close(key, bar, bar.open, bar.end, ["fixed_stop"])
    assert engine.remaining_open_lots(meta, DAY) == 0
    assert engine.remaining_open_lots(meta, "2026-01-09") == 3
    retry = example_signal()
    engine.admit_opportunity(retry, meta, bar.open, DAY, bar.end)
    assert not retry["risk_pass"]
    assert "daily_open_limit" in retry["risk_rejections"]


def test_fill_rechecks_daily_capacity_and_minimum_quantity(market):
    engine = limited_engine(market)
    key = "aa2603.SHFE"
    meta, _ = engine.parameters.resolve(key, at(DAY, "09:20"))
    signal = example_signal()
    bar = engine.data.by_day[(DAY, key)][at(DAY, "09:21")]
    engine.admit_opportunity(signal, meta, bar.open, DAY, at(DAY, "09:20"))
    engine.opened_lots[(DAY, key)] = 2  # Remaining capacity changes before a fill.
    engine.fill_open(key, bar, bar.datetime)
    assert engine.state(key).position is None
    assert signal["fill_rejections"] == ["minimum_open_lots"]
    assert engine.opened_lots[(DAY, key)] == 2


def test_unavailable_first_rank_never_backfills_second(market):
    cfg = copy.deepcopy(market.cfg)
    meta = copy.deepcopy(
        next(m for m in cfg["metadata"]["contracts"] if m["product"] == "aa")
    )
    meta.update(product="ee", symbol="ee2603")
    cfg["metadata"]["contracts"].append(meta)
    cfg["strategy"]["fixed_ticks"]["ee"] = copy.deepcopy(
        cfg["strategy"]["fixed_ticks"]["aa"]
    )
    bars = list(market.bars) + [
        replace(b, product="ee", symbol="ee2603")
        for b in market.bars
        if b.product == "aa"
    ]
    data = Dataset(bars, cfg, market.quality)
    diagnostic = profile(data, enabled={"ee", "bb", "cc", "dd"})
    engine = PortfolioBacktest(data, diagnostic)
    result = engine.run(DAY, DAY)
    longs = [
        r
        for r in result["daily_candidates"]
        if r["group"] == "commodity" and r["direction"] == "LONG"
    ]
    assert [(r["product"], r["rank"], r["selected"]) for r in longs] == [
        ("aa", 1, True),
        ("ee", 2, False),
    ]
    assert any(
        s["trigger"] and s["product"] == "aa" and not s["execution_pass"]
        for s in result["signals"]
    )
    assert not any(t["product"] in {"aa", "ee"} for t in result["trades"])
    assert any(t["product"] == "bb" for t in result["trades"])
    assert not any(
        o["contract"] in {"aa2603.SHFE", "ee2603.SHFE"} for o in result["orders"]
    )
    # Neither the original signal metadata nor the pool was patched to bypass costs.
    assert data.cfg == cfg
    assert data.pool(DAY)[0] == engine.data.pool(DAY)[0]


def test_training_insufficiency_cannot_be_overridden_by_manual_ticks(market):
    cfg = profile(market, missing={"bb": ["no_positive_volume_training_minutes"]})
    resolver = ExecutionParameters(cfg, market.metadata)
    meta, reasons = resolver.resolve("bb2603.SHFE", at(DAY, "10:00"))
    assert meta is None
    assert "training_insufficient:no_positive_volume_training_minutes" in reasons
    result = PortfolioBacktest(market, cfg).run(DAY, DAY)
    assert not any(t["product"] == "bb" for t in result["trades"])
    assert any(
        r["product"] == "bb" and r["selected"] for r in result["daily_candidates"]
    )


@pytest.mark.parametrize(
    "mutation",
    ["same_day", "late_available", "later_training", "new_ticks", "forged_ready"],
)
def test_diagnostic_rejects_invalid_temporal_or_training_freeze(market, mutation):
    cfg = profile(market)
    q = cfg["execution"]["qualification"]
    if mutation == "same_day":
        q["rules"][0]["source_date"] = DAY
    elif mutation == "late_available":
        q["rules"][0]["available_at"] = at(DAY, "16:00").isoformat()
    elif mutation == "later_training":
        q["training_window"] = {"start": "2026-01-05", "end": DAY}
    elif mutation == "new_ticks":
        cfg["strategy"]["fixed_ticks"]["aa"]["stop_loss_ticks"] += 1
    else:
        q["training_ready_products"].append("xx")
    refresh(cfg)
    with pytest.raises(ResearchError):
        ExecutionParameters(cfg, market.metadata)


def test_missing_session_rule_does_not_carry_previous_parameters(market):
    cfg = profile(market)
    resolver = ExecutionParameters(cfg, market.metadata)
    assert resolver.resolve("aa2603.SHFE", at(DAY, "10:00"))[0]
    assert resolver.resolve("aa2603.SHFE", at("2026-01-09", "10:00")) == (
        None,
        ["exact_session_execution_rule_missing"],
    )
    with pytest.raises(ResearchError, match="锁定测试"):
        resolver.preflight({"aa"}, DAY, "2026-01-09")


def test_changed_training_data_cannot_reuse_frozen_diagnostic_qualification(market):
    cfg = profile(market)
    altered = Dataset(
        [
            replace(b, volume=b.volume + 1) if b.trading_day == "2026-01-07" else b
            for b in market.bars
        ],
        market.cfg,
        market.quality,
    )
    with pytest.raises(ResearchError, match="训练数据已改变"):
        ExecutionParameters(cfg, altered.metadata).preflight({"aa"}, DAY, DAY, altered)


def test_stream_cache_equivalent_for_all_frames_and_published_atomically(
    market, tmp_path
):
    import pandas as pd

    original = Features(market, tmp_path)
    restored = Features(market, tmp_path)
    for identity in original.frames:
        pd.testing.assert_frame_equal(
            original.frames[identity],
            restored.frames[identity],
            check_dtype=False,
            check_freq=False,
        )
    assert len(list(tmp_path.glob("*.jsonl.gz"))) == 1
    assert not list(tmp_path.glob("*.partial"))
    path = next(tmp_path.glob("*.jsonl.gz"))
    with pytest.raises(ResearchError, match="指纹"):
        list(read_frames(path, "different-key"))


def test_matching_window_releases_history_without_changing_indicator_values(market):
    import pandas as pd

    cfg = profile(market)
    complete = Features(market)
    engine = PortfolioBacktest(market, cfg)
    engine.run(DAY, DAY)
    for identity, frame in engine.features.frames.items():
        assert len(frame) < len(complete.frames[identity])
        for clock in ("09:15", "10:15", "13:35", "14:50"):
            key, minutes = identity
            a = engine.features.latest(key, minutes, at(DAY, clock))
            b = complete.latest(key, minutes, at(DAY, clock))
            pd.testing.assert_series_equal(a, b)


def test_equal_configuration_reuses_dataset_but_variant_has_own_signal_configuration(
    market,
):
    assert scope_data(market, market.cfg, "shared")[0] is market
    assert PortfolioBacktest(market, market.cfg).data is market
    original = copy.deepcopy(market.cfg)
    variant = copy.deepcopy(market.cfg)
    variant["strategy"]["k"] = 2
    scoped, config = scope_data(market, variant, "shared")
    assert scoped is not market and scoped.cfg["strategy"]["k"] == 2
    assert PortfolioBacktest(market, config).data.cfg["strategy"]["k"] == 2
    assert market.cfg == original


def test_budget_failure_never_publishes_partial_indicator_cache(market, tmp_path):
    path = tmp_path / "failed.jsonl.gz"
    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": 1, "min_free_bytes": 0}
    )
    with pytest.raises(ResearchError, match="空间预算"):
        write_frames(path, "SYNTHETIC_TEST_ONLY", Features(market).frames, budget)
    assert not path.exists()


def test_exit_without_parameters_retains_position_and_pending_risk(market):
    cfg = profile(market, enabled={"aa"})
    engine = PortfolioBacktest(market, cfg)
    key = "aa2603.SHFE"
    meta = engine.parameters.resolve(key, at(DAY, "09:15"))[0]
    assert meta["execution_reference"]["source_date"] == "2026-01-07"
    st = engine.state(key)
    st.name = "LONG"
    st.position = Position(
        key, meta, 1, 1, 100, 100, at(DAY, "09:15"), DAY, 90, 120, 1, 100, 100, {}
    )
    bar = market.by_day[(DAY, key)][at(DAY, "09:15")]
    assert (
        engine.close(key, bar, 100, at("2026-01-09", "09:00"), ["time_force"]) is False
    )
    assert st.name == "EXIT_PENDING" and st.position.quantity == 1
    assert not engine.trades
    assert engine.events[-1]["action"] == "exit_execution_unavailable"


def test_pending_entry_cancelled_if_next_available_minute_rule_has_expired(market):
    # A missing next minute leaves the order pending; qualification must be checked again.
    bars = [
        b
        for b in market.bars
        if not (b.product == "aa" and b.datetime == at(DAY, "09:15"))
    ]
    data = Dataset(bars, market.cfg, market.quality)
    cfg = profile(market, enabled={"aa"})
    cfg["execution"]["qualification"]["rules"][0]["effective_to"] = at(
        DAY, "09:16"
    ).isoformat()
    refresh(cfg)
    result = PortfolioBacktest(data, cfg).run(DAY, DAY)
    assert any(e["action"] == "entry_requested" for e in result["events"])
    assert any(e.get("reason") == "fill_execution_recheck" for e in result["events"])
    assert not result["trades"]


def test_all_qualified_diagnostic_matches_original_engine(market):
    cfg = profile(market)
    result = PortfolioBacktest(market, cfg).run(DAY, DAY)
    formal_cfg = copy.deepcopy(cfg)
    formal_cfg.pop("execution")
    formal = PortfolioBacktest(market, formal_cfg).run(DAY, DAY)
    fields = [
        "contract",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "quantity",
        "net_pnl",
        "exit_reason",
    ]
    assert [{k: t[k] for k in fields} for t in result["trades"]] == [
        {k: t[k] for k in fields} for t in formal["trades"]
    ]
    assert result["daily_candidates"] == formal["daily_candidates"]
    assert cfg["risk"] == formal_cfg["risk"]


def test_diagnostic_future_prices_leave_prior_candidates_signals_and_fills_unchanged(
    market,
):
    cfg = profile(market)
    cutoff = at(DAY, "11:00")
    original = PortfolioBacktest(market, cfg).run(DAY, DAY)
    altered = Dataset(
        [
            replace(
                b,
                open=b.open + 20,
                high=b.high + 20,
                low=b.low + 20,
                close=b.close + 20,
                open_interest=b.open_interest + 500,
            )
            if b.datetime >= cutoff
            else b
            for b in market.bars
        ],
        market.cfg,
        market.quality,
    )
    after = PortfolioBacktest(altered, cfg).run(DAY, DAY)
    assert original["daily_candidates"] == after["daily_candidates"]
    signal_fields = [
        "time",
        "filters",
        "rejections",
        "trigger",
        "snapshot",
        "execution_pass",
        "execution_rejections",
    ]
    assert [
        {k: r[k] for k in signal_fields}
        for r in original["signals"]
        if r["time"] < cutoff.isoformat()
    ] == [
        {k: r[k] for k in signal_fields}
        for r in after["signals"]
        if r["time"] < cutoff.isoformat()
    ]
    for field, timestamp in (
        ("events", "time"),
        ("equity", "time"),
        ("trades", "exit_time"),
    ):
        assert [r for r in original[field] if r[timestamp] < cutoff.isoformat()] == [
            r for r in after[field] if r[timestamp] < cutoff.isoformat()
        ]


def test_formal_guard_and_final_test_lock_remain_strict(market, tmp_path):
    cfg = profile(market)
    formal_cfg = copy.deepcopy(cfg)
    formal_cfg.pop("execution")
    formal_cfg.update(
        synthetic=False, research_blockers=["historical_universe_unverified"]
    )
    with pytest.raises(ResearchError, match="正式回测配置缺失"):
        ExecutionParameters(formal_cfg, market.metadata).preflight({"aa"}, DAY, DAY)
    with pytest.raises(ResearchError, match="选K"):
        sweep(market, cfg, tmp_path, budget=1)
    directory, result = run_one(
        market, cfg, tmp_path, cfg["splits"]["validation"], make_report=True
    )
    assert result["status"] == "completed"
    assert result["execution_mode"] == "diagnostic"
    assert (directory / "candidate_execution.csv").exists()
    with pytest.raises(ResearchError, match="诊断执行"):
        freeze_run(directory, tmp_path / "frozen.json")
    p = tmp_path / "diagnostic_config.json"
    p.write_text(json.dumps(cfg))
    assert main(["backtest", "--config", str(p), "--synthetic"]) == 2
    assert (
        main(
            [
                "diagnostic-backtest",
                "--config",
                str(p),
                "--split",
                "test",
                "--synthetic",
            ]
        )
        == 2
    )


def test_report_reuses_existing_result_and_checks_shared_integrity(
    market, tmp_path, monkeypatch
):
    from research import reporting

    cfg = profile(market)
    directory, result = run_one(market, cfg, tmp_path, cfg["splits"]["validation"])

    def no_duplicate_read(_):
        raise AssertionError("report must reuse the completed result")

    monkeypatch.setattr("research.storage.read_result", no_duplicate_read)
    supplied = market.until(DAY)
    assert reporting.report_run(directory, supplied, result=result).exists()
    reference = json.loads((directory / "data_reference.json").read_text())
    obj = (directory / reference["object"]).resolve()
    with obj.open("ab") as stream:
        stream.write(b"CORRUPTED_TEST_ONLY")
    with pytest.raises(ResearchError, match="SHA256"):
        reporting.report_run(directory, supplied, result=result)


def test_contract_units_and_future_specification_rejected():
    row = {
        "data_standard": "2023-03-29",
        "ContractSize": "5吨/手",
        "MinimumPriceFluctuation": "10元/吨",
        "PriceQuotation": "元（人民币）/吨",
        "ContractMultiplier": "",
    }
    text = "<script>let pageList = [" + json.dumps(row) + ",]</script>"
    result = contract_specifications(text, "2026-09-11")
    assert (result["tick_size"], result["value_per_price"]) == (10, 5)
    with pytest.raises(ResearchError, match="未来"):
        contract_specifications(text, "2020-01-01")
    row["PriceQuotation"] = "元/千克"
    with pytest.raises(ResearchError, match="单位"):
        contract_specifications(
            "<script>let pageList = [" + json.dumps(row) + "]</script>", "2026-09-11"
        )


@pytest.mark.parametrize(
    "symbol,multiplier,open_price,close_price,expected",
    [
        ("al2611", 5, 25000, 25005, (3, 3, 3, 6)),
        ("au2612", 1000, 1000, 1001, (20, 0, 20, 20)),
        ("rb2701", 10, 3000, 3001, (3, 3.001, 3.001, 6.001)),
        ("cu2611", 5, 100000, 100010, (25, 50.005, 25.0025, 75.005)),
    ],
)
def test_archived_real_fees_against_literal_independent_round_trip(
    symbol, multiplier, open_price, close_price, expected
):
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "shfe_execution_reference.json"
        ).read_text()
    )
    raw = fixture["fee_rows"][symbol]
    meta = {
        "symbol": symbol,
        "value_per_price": multiplier,
        "fees": [{"effective_from": "2026-09-11", **fee_reference(raw)}],
    }
    opening = fee(meta, "2026-09-11", "open", open_price, 1)
    today = fee(meta, "2026-09-11", "close_today", close_price, 1)
    yesterday = fee(meta, "2026-09-11", "close_yesterday", close_price, 1)
    assert (opening, today, yesterday, opening + today) == pytest.approx(expected)
    assert fixture["provenance"] == "REAL_OFFICIAL_PARAMETER_REFERENCE"


def test_real_holiday_margin_changes_apply_after_settlement_not_at_same_day_open():
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "shfe_execution_reference.json"
        ).read_text()
    )
    for row in fixture["margin_cases"]:
        previous = {
            "exchange_speculation_margin": {
                "long": row["previous_settlement"],
                "short": row["previous_settlement"],
            }
        }
        check = timing_check(
            row["date"],
            row["contract"],
            previous,
            {row["contract"].split(".")[0]: row["intraday_raw"]},
        )
        assert check["matches"]
        if row["changes_after_close"]:
            assert row["current_settlement"] != check["actual_intraday_margin"]["long"]
        assert check["used_in_decisions"] is False
