"""SYNTHETIC_TEST_ONLY: audit conservation and training isolation, no PnL claims."""

import copy
import json
from dataclasses import replace

import pytest

from research.config import ResearchError, execution_gaps, read_config, validate_config
from research.data import DailyObservation, Dataset, load_data
from research.experiments import apply_calibration, calibrate_ticks
from research.fixtures import create_fixture
from research.readiness import lock_splits, reconcile_products, volume_audit


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    base = load_data(
        create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY_review"))
    )
    daily = [
        DailyObservation(
            day,
            bars[0].exchange,
            bars[0].symbol,
            bars[0].product,
            sum(b.volume for b in bars),
            bars[-1].open_interest,
            provenance="SYNTHETIC_TEST_ONLY",
        )
        for (day, _), records in sorted(base.by_day.items())
        for bars in [sorted(records.values(), key=lambda b: b.datetime)]
    ]
    return Dataset(base.bars, base.cfg, base.quality, daily)


def catalogue(cfg):
    products = sorted(
        {m["exchange"] + "." + m["product"] for m in cfg["metadata"]["contracts"]}
    )
    return {
        "products": products,
        "indexed_products": products + ["CZCE.LR"],
        "historical_universe_verified": False,
    }


def test_product_day_conservation_does_not_double_count_competitor_contracts(sample):
    cfg = copy.deepcopy(sample.cfg)
    extra = copy.deepcopy(cfg["metadata"]["contracts"][0])
    extra["symbol"] = "aa2604"
    cfg["metadata"]["contracts"].append(extra)
    daily = list(sample.daily) + [
        replace(r, symbol="aa2604", open_interest=1)
        for r in sample.daily
        if r.product == "aa"
    ]
    data = Dataset(sample.bars, cfg, daily=daily)
    days = ["2026-01-07", "2026-01-08"]
    summary, rows, totals, selected = reconcile_products(data, days, catalogue(cfg))
    assert summary["equations_hold"]
    assert summary["initial_product_days"] == 8
    assert len(rows) == len(selected) == 8
    assert all(r["initial"] == r["pool_excluded"] + r["pool_selected"] for r in totals)
    assert all(
        r["pool_selected"] == r["ranking_excluded"] + r["directional_ranked"]
        for r in totals
    )
    assert summary["index_without_real_contract_catalogue"] == ["CZCE.LR"]
    assert summary["historical_universe_verified"] is False


def test_audit_distinguishes_zero_previous_oi_from_missing_daily_and_zero_opening(
    sample,
):
    daily = [
        replace(r, open_interest=0) if r.product == "aa" else r for r in sample.daily
    ]
    bars = [
        replace(b, volume=0)
        if b.product == "bb" and b.trading_day == "2026-01-08"
        else b
        for b in sample.bars
    ]
    data = Dataset(bars, sample.cfg, daily=daily)
    summary, rows, _, _ = reconcile_products(
        data, ["2026-01-08"], catalogue(sample.cfg)
    )
    aa = next(r for r in rows if r["product_id"] == "SHFE.aa")
    bb = next(r for r in rows if r["product_id"] == "SHFE.bb")
    assert (
        aa["stage"] == "pool_excluded"
        and aa["primary_reason"] == "no_positive_previous_oi"
    )
    assert (
        bb["stage"] == "ranking_excluded"
        and bb["primary_reason"] == "opening_no_volume"
    )
    assert summary["initial_product_days"] == 4
    assert summary["pool_excluded_product_days"] == 1
    assert summary["ranking_excluded_product_days"] == 1
    assert summary["directional_ranked_product_days"] == 2
    missing = Dataset(bars, sample.cfg, daily=[r for r in daily if r.product != "aa"])
    row = next(
        r
        for r in reconcile_products(missing, ["2026-01-08"], catalogue(sample.cfg))[1]
        if r["product_id"] == "SHFE.aa"
    )
    assert row["primary_reason"] == "previous_daily_observation_missing_or_incomplete"


def test_audit_keeps_all_independent_rank_rejections(sample):
    bars = [
        replace(b, volume=0, tradable=False, open_interest=None)
        if b.product == "aa" and b.trading_day == "2026-01-08"
        else b
        for b in sample.bars
    ]
    data = Dataset(bars, sample.cfg, daily=sample.daily)
    row = next(
        r
        for r in reconcile_products(data, ["2026-01-08"], catalogue(sample.cfg))[1]
        if r["product_id"] == "SHFE.aa"
    )
    assert set(row["reasons"]) == {
        "opening_oi_missing",
        "opening_no_volume",
        "opening_not_tradable",
    }


def test_month_inside_roll_warmup_is_not_classified_as_research(sample):
    rows = list(sample.by_contract["aa2603.SHFE"][:10])
    rows += [
        replace(b, volume=0)
        for b in sample.by_contract["aa2603.SHFE"]
        if b.trading_day == "2026-01-08"
    ]
    data = Dataset(rows, sample.cfg)
    plan = [
        {"contract": "aa2603.SHFE", "trading_day": "2026-01-05", "purpose": "warmup"},
        {"contract": "aa2603.SHFE", "trading_day": "2026-01-08", "purpose": "research"},
    ]
    summary, _, contract_days = volume_audit(data, plan)
    assert summary["by_purpose"]["warmup"]["minutes"] == 10
    assert summary["by_purpose"]["research"]["zero_volume_ratio"] == 1
    assert summary["by_purpose"]["research"]["whole_day_zero_volume_days"] == 1
    assert not summary["unplanned_contract_days"]
    assert (
        next(r for r in contract_days if r["purpose"] == "warmup")["calendar_split"]
        == "train"
    )
    with pytest.raises(ResearchError, match="合约日重复"):
        volume_audit(data, plan + [plan[0]])


def test_partial_calibration_preserves_ready_products_and_never_supplies_fake_ticks(
    sample,
):
    cfg = copy.deepcopy(sample.cfg)
    cfg["strategy"]["fixed_ticks"] = {}
    bars = [replace(b, volume=0) if b.product == "aa" else b for b in sample.bars]
    data = Dataset(bars, cfg, daily=sample.daily).until(cfg["splits"]["train"]["end"])
    calibration = calibrate_ticks(data, cfg)
    assert calibration["status"] == "incomplete"
    assert set(calibration["products"]) == {"bb", "cc", "dd"}
    assert calibration["missing_products"]["aa"]["reasons"] == [
        "no_positive_volume_training_minutes"
    ]
    updated = apply_calibration(cfg, calibration)
    assert "aa" not in updated["strategy"]["fixed_ticks"]
    assert any("aa: 缺少正整数" in gap for gap in execution_gaps(updated, {"aa", "bb"}))


def test_calibration_never_samples_not_selected_contracts_or_month_inside_warmup(
    sample,
):
    cfg = copy.deepcopy(sample.cfg)
    extra = copy.deepcopy(cfg["metadata"]["contracts"][0])
    extra["symbol"] = "aa2604"
    cfg["metadata"]["contracts"].append(extra)
    daily = list(sample.daily) + [
        replace(r, symbol="aa2604", open_interest=1)
        for r in sample.daily
        if r.product == "aa"
    ]
    other = [
        replace(b, symbol="aa2604", high=b.high + 1000, low=1)
        for b in sample.bars
        if b.product == "aa"
    ]
    end = cfg["splits"]["train"]["end"]
    expected = calibrate_ticks(Dataset(sample.bars, cfg, daily=daily).until(end), cfg)
    observed = calibrate_ticks(
        Dataset(list(sample.bars) + other, cfg, daily=daily).until(end), cfg
    )
    assert observed["products"] == expected["products"]
    assert observed["selection_hash"] == expected["selection_hash"]
    assert (
        observed["diagnostics"]["aa"]["acquired_minutes"]
        > expected["diagnostics"]["aa"]["acquired_minutes"]
    )


def test_product_absent_from_training_bars_still_gets_explicit_readiness_reason(sample):
    cfg = copy.deepcopy(sample.cfg)
    extra = copy.deepcopy(cfg["metadata"]["contracts"][0])
    extra.update(symbol="ee2603", product="ee")
    cfg["metadata"]["contracts"].append(extra)
    training = Dataset(sample.bars, cfg, daily=sample.daily).until(
        cfg["splits"]["train"]["end"]
    )
    row = calibrate_ticks(training, cfg)["missing_products"]["ee"]
    assert row["acquired_minutes"] == 0
    assert row["reasons"] == ["no_selected_training_minutes"]


def test_future_daily_input_rejected_before_feature_calculation(sample, monkeypatch):
    from research import experiments

    end = sample.cfg["splits"]["train"]["end"]
    truncated = sample.until(end)
    poisoned = Dataset(truncated.bars, sample.cfg, daily=sample.daily)
    monkeypatch.setattr(
        experiments,
        "Features",
        lambda _: pytest.fail("future input reached indicators"),
    )
    with pytest.raises(ResearchError, match="物理截断"):
        calibrate_ticks(poisoned, sample.cfg)


def test_split_lock_rejects_boundary_and_calendar_changes_but_allows_declared_folds(
    sample, tmp_path
):
    cfg = copy.deepcopy(sample.cfg)
    path = tmp_path / "split_lock.json"
    locked = lock_splits(cfg, path)
    assert lock_splits(cfg, path) == locked
    cfg["split_lock"] = locked
    validate_config(cfg)
    changed = copy.deepcopy(cfg)
    changed["splits"]["train"]["start"] = "2026-01-06"
    with pytest.raises(ResearchError, match="拒绝移动边界"):
        validate_config(changed)
    with pytest.raises(ResearchError, match="不覆盖"):
        lock_splits(changed, path)
    changed = copy.deepcopy(cfg)
    changed["calendar"]["trading_days"].remove("2026-01-06")
    with pytest.raises(ResearchError, match="交易日历已改变"):
        validate_config(changed)
    fold = {
        "train": {"start": "2026-01-06", "end": "2026-01-07"},
        "validation": copy.deepcopy(cfg["splits"]["validation"]),
    }
    cfg["experiments"]["windows"] = [fold]
    cfg["walk_forward_window"] = fold
    cfg["splits"].update(fold)
    validate_config(cfg)
    cfg["split_lock"] = "split_lock.json"
    snapshot = tmp_path / "config.json"
    snapshot.write_text(json.dumps(cfg))
    assert read_config(snapshot)["split_lock"]["kind"] == "research_split_only"


def test_calibration_setting_changes_require_new_training_candidates(sample):
    cfg = sample.cfg
    calibration = calibrate_ticks(sample.until(cfg["splits"]["train"]["end"]), cfg)
    changed = copy.deepcopy(cfg)
    changed["strategy"]["atr_period"] += 1
    with pytest.raises(ResearchError, match="候选生成配置已改变"):
        apply_calibration(changed, calibration)


def test_partial_calibration_cli_saves_diagnostics_and_returns_incomplete(
    sample, tmp_path, monkeypatch
):
    from research import __main__ as cli

    cfg = copy.deepcopy(sample.cfg)
    data = Dataset(
        [replace(b, volume=0) if b.product == "aa" else b for b in sample.bars],
        cfg,
        daily=sample.daily,
    )
    monkeypatch.setattr(cli, "read_config", lambda _: cfg)

    def training_only(_, cutoff=None):
        assert cutoff == cfg["splits"]["train"]["end"]
        return data.until(cutoff)

    monkeypatch.setattr(cli, "load_data", training_only)
    assert cli.main(["calibrate-ticks", "--synthetic", "--output", str(tmp_path)]) == 1
    saved = json.loads((tmp_path / "training_tick_candidates.json").read_text())
    assert saved["status"] == "incomplete" and saved["locked_test_read"] is False
    assert set(saved["products"]) == {"bb", "cc", "dd"}
    assert saved["reproducibility"]["code_hash"]
