"""SYNTHETIC_TEST_ONLY: small deterministic tests, never return-validation experiments."""

import copy
import csv
import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

from research.acquisition import (
    PublicDownloader,
    catalogue_contracts,
    edb_url,
    iter_catalog,
    minute_plan,
    monthly_calendar,
    normalize_daily,
    normalize_minutes,
    protect_generated_config,
)
from research.calendar import Calendar, at
from research.config import ResearchError, digest, read_config
from research.data import DailyObservation, Dataset, file_sha256, load_data
from research.experiments import run_one, scope_data
from research.fixtures import create_fixture
from research.signals import rank_candidates
from research.storage import (
    SpaceBudget,
    directory_bytes,
    restore_dataset,
    store_dataset,
)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return load_data(
        create_fixture(tmp_path_factory.mktemp("SYNTHETIC_TEST_ONLY_monthly"))
    )


def observations(base):
    return [
        DailyObservation(
            day,
            key.split(".")[1],
            key.split(".")[0],
            base.metadata.get(key, day)["product"],
            sum(b.volume for b in bars.values()),
            sorted(bars.values(), key=lambda b: b.datetime)[-1].open_interest,
            provenance="SYNTHETIC_TEST_ONLY",
        )
        for (day, key), bars in base.by_day.items()
    ]


def test_previous_daily_oi_selects_competitor_without_its_minute_history(base):
    cfg = copy.deepcopy(base.cfg)
    contract = copy.deepcopy(cfg["metadata"]["contracts"][0])
    contract["symbol"] = "aa2604"
    cfg["metadata"]["contracts"].append(contract)
    daily = observations(base)
    daily.append(
        DailyObservation(
            "2026-01-07",
            "SHFE",
            "aa2604",
            "aa",
            500,
            999999,
            provenance="SYNTHETIC_TEST_ONLY",
        )
    )
    data = Dataset(base.bars, cfg, daily=daily)
    pool, _ = data.pool("2026-01-08")
    aa = next(r for r in pool if r["product"] == "aa")
    assert aa["contract"] == "aa2604.SHFE"
    assert aa["selection_day"] == "2026-01-07"
    ranked, rejected = rank_candidates(
        data, "2026-01-08", pool, at("2026-01-08", "09:38"), "ALL"
    )
    assert not any(r["product"] == "aa" for r in ranked)  # No replacement with aa2603.
    assert any(r["contract"] == "aa2604.SHFE" for r in rejected)


def test_daily_missing_and_incomplete_never_fall_back_to_older_or_minute_data(base):
    cfg = copy.deepcopy(base.cfg)
    cfg["data"]["daily_sources"] = [
        {"format": "csv", "path": "NOT_READ_IN_DIRECT_DATASET"}
    ]
    daily = [r for r in observations(base) if r.trading_day != "2026-01-07"]
    data = Dataset(base.bars, cfg, daily=daily)
    assert not data.pool("2026-01-08")[0]
    assert all(
        r["reason"] == "previous_daily_observation_missing_or_incomplete"
        for r in data.pool("2026-01-08")[1]
    )
    daily = [
        replace(r, complete=False) if r.trading_day == "2026-01-07" else r
        for r in observations(base)
    ]
    assert not Dataset(base.bars, cfg, daily=daily).pool("2026-01-08")[0]


def test_future_daily_changes_do_not_change_prior_pool_or_truncated_fingerprint(base):
    daily = observations(base)
    before = Dataset(base.bars, base.cfg, daily=daily)
    after = Dataset(
        base.bars,
        base.cfg,
        daily=[
            replace(r, open_interest=1e10) if r.trading_day >= "2026-01-08" else r
            for r in daily
        ],
    )
    assert before.pool("2026-01-08") == after.pool("2026-01-08")
    assert (
        before.until("2026-01-07").fingerprint == after.until("2026-01-07").fingerprint
    )
    assert before.fingerprint != after.fingerprint
    assert base.fingerprint == digest([b.wire() for b in base.bars])


def test_daily_inputs_preserved_in_scope_and_execution_clone(base):
    from research.execution import PortfolioBacktest

    data = Dataset(base.bars, base.cfg, daily=observations(base))
    scoped, cfg = scope_data(data, base.cfg, "financial")
    assert scoped.daily and all(r.exchange == "CFFEX" for r in scoped.daily)
    engine = PortfolioBacktest(scoped, cfg)
    assert engine.data.daily == scoped.daily
    assert engine.data.pool("2026-01-08") == scoped.pool("2026-01-08")


def test_daily_import_requires_complete_and_does_not_parse_locked_oi(base, tmp_path):
    cfg = copy.deepcopy(base.cfg)
    rows = [r.wire() for r in observations(base)]
    for row in rows:
        if row["trading_day"] == "2026-01-09":
            row["open_interest"] = "LOCKED_FUTURE_MUST_NOT_PARSE"
    path = tmp_path / "SYNTHETIC_TEST_ONLY_daily.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    cfg["data"]["daily_sources"] = [{"format": "csv", "path": str(path)}]
    clean = load_data(cfg, cutoff="2026-01-07")
    assert clean.daily and not clean.quality["errors"]
    assert all(r.trading_day <= "2026-01-07" for r in clean.daily)
    assert load_data(cfg).quality["errors"]
    cfg["data"]["daily_sources"][0]["path"] = "SYNTHETIC_TEST_ONLY_daily.csv"
    cfg["data"]["sources"] = []
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert read_config(tmp_path / "config.json")["data"]["daily_sources"][0][
        "path"
    ] == str(path)


def test_duplicate_daily_is_rejected_instead_of_overwritten(base):
    daily = observations(base)
    with pytest.raises(ResearchError, match="重复合约日线"):
        Dataset(base.bars, base.cfg, daily=daily + [daily[0]])


def test_shared_snapshot_one_object_across_k_and_group_scope_and_daily_roundtrip(
    base, tmp_path
):
    data = Dataset(base.bars, base.cfg, daily=observations(base))
    root = tmp_path / "objects"
    for scope in ["shared", "financial"]:
        scoped, cfg = scope_data(data, base.cfg, scope)
        run = tmp_path / scope
        run.mkdir()
        store_dataset(data, run, root, scoped.fingerprint)
        restored = restore_dataset(run, cfg)
        assert restored.fingerprint == scoped.fingerprint
        assert restored.daily == scoped.daily
    assert len(list(root.glob("*.jsonl.gz"))) == 1
    assert not list(tmp_path.rglob("normalized_data.json.gz"))
    obj = next(root.glob("*.jsonl.gz"))
    obj.write_bytes(obj.read_bytes() + b"CORRUPT")
    with pytest.raises(ResearchError, match="SHA256"):
        restore_dataset(tmp_path / "shared", base.cfg)
    with pytest.raises(ResearchError, match="损坏"):
        store_dataset(data, tmp_path / "shared", root, data.fingerprint)


def test_legacy_snapshot_still_rebuilds(base, tmp_path):
    (tmp_path / "normalized_data.json.gz").write_bytes(
        gzip.compress(json.dumps([b.wire() for b in base.bars]).encode())
    )
    assert restore_dataset(tmp_path, base.cfg).fingerprint == base.fingerprint


def test_compact_run_and_report_freeze_read_compressed_result(base, tmp_path):
    from research.experiments import freeze_run
    from research.reporting import report_run
    from research.storage import read_result

    cfg = copy.deepcopy(base.cfg)
    cfg["storage"] = {
        "compact_results": True,
        "shared_root": str(tmp_path / "datasets"),
    }
    data = Dataset(base.bars, cfg, quality=base.quality, daily=observations(base))
    directory, result = run_one(
        data, cfg, tmp_path / "runs", cfg["splits"]["validation"]
    )
    assert result["status"] == "completed"
    assert (directory / "result.json.gz").exists()
    assert (directory / "signals.csv.gz").exists()
    assert not (directory / "normalized_data.json.gz").exists()
    assert not (directory / "signals.json").exists()
    assert read_result(directory)["trades"] == result["trades"]
    assert report_run(directory).exists()
    assert freeze_run(directory, tmp_path / "frozen.json")["configuration"]["synthetic"]


def test_space_budget_counts_temp_and_combined_roots_and_free_reserve(
    tmp_path, monkeypatch
):
    import shutil

    from research import storage

    a, b = tmp_path / "raw", tmp_path / "results"
    a.mkdir()
    b.mkdir()
    (a / "new.csv.partial").write_bytes(b"x" * 30)
    (b / "cached.gz").write_bytes(b"x" * 30)
    budget = SpaceBudget(
        {"roots": [str(a), str(b)], "max_bytes": 100, "min_free_bytes": 10}
    )
    assert directory_bytes([a, b]) == 60
    with pytest.raises(ResearchError, match="空间预算"):
        budget.check(a / "x", reserve=41)
    with pytest.raises(ResearchError, match="目录内"):
        budget.check(tmp_path / "outside")
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: usage_type(100, 85, 15))
    with pytest.raises(ResearchError, match="安全余量"):
        budget.check(a / "x", reserve=6)


def test_anonymous_downloader_resume_checks_url_hash_and_does_not_redownload(tmp_path):
    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": 100000, "min_free_bytes": 0}
    )
    client = PublicDownloader(tmp_path, budget)
    url = edb_url("SHFE.rb2701", 60, "2026-09-01 09:00:00", "2026-09-01 15:00:00")
    path = tmp_path / "cached.csv"
    path.write_text("SYNTHETIC_TEST_ONLY\n")
    path.with_suffix(".csv.receipt.json").write_text(
        json.dumps({"url": url, "sha256": file_sha256(path)})
    )
    assert client.fetch(url, "cached.csv") == (path, True)
    assert client.request_count == 0
    path.write_text("CHANGED_SYNTHETIC_TEST_ONLY")
    with pytest.raises(ResearchError, match="校验失败"):
        client.fetch(url, "cached.csv")
    with pytest.raises(ResearchError, match="匿名"):
        client.fetch(url + "&token=do_not_read", "x.csv")
    with pytest.raises(ResearchError, match="真实期货"):
        edb_url("KQ.m@SHFE.rb", 60, "a", "b")


def test_streamed_catalog_requires_complete_and_filters_options_old_decade_codes(
    tmp_path,
):
    from datetime import datetime

    from research.calendar import TZ

    calendar = monthly_calendar("2026-09", ["2026-09-25"], 5)
    common = {
        "class": "FUTURE",
        "price_tick": 1,
        "volume_multiple": 10,
        "trading_time": {
            "day": [
                ["09:00:00", "10:15:00"],
                ["10:30:00", "11:30:00"],
                ["13:30:00", "15:00:00"],
            ]
        },
    }
    items = {
        "CZCE.SR609": {
            **common,
            "expire_datetime": datetime(2016, 9, 14, tzinfo=TZ).timestamp(),
        },
        "SHFE.aa2610": {
            **common,
            "expire_datetime": datetime(2026, 10, 15, tzinfo=TZ).timestamp(),
        },
        "SHFE.aa2610C2000": {**common, "class": "FUTURE_OPTION"},
        "KQ.m@SHFE.aa": {"class": "FUTURE_CONT"},
        "IGNORED": {"class": "OPTION", "long_text": "测" * 80000},
    }
    path = tmp_path / "SYNTHETIC_TEST_ONLY_catalog.json"
    path.write_text(json.dumps(items, ensure_ascii=False))
    assert dict(iter_catalog(path)) == items
    contracts, _, _ = catalogue_contracts(path, calendar, "2026-09")
    assert [r["provider_symbol"] for r in contracts] == ["SHFE.aa2610"]
    assert contracts[0]["listed"] is None and not contracts[0]["verified"]
    path.write_text(path.read_text()[:-2])
    with pytest.raises(ResearchError, match="不完整|格式错误"):
        list(iter_catalog(path))


def test_calendar_and_roll_warmup_are_bounded_actual_trading_days():
    calendar = monthly_calendar("2026-09", ["2026-09-25"], 5)
    assert "2026-09-25" not in calendar["trading_days"]
    assert calendar["trading_days"][:6] == [
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
    ]
    with pytest.raises(ResearchError, match="5"):
        monthly_calendar("2026-09", ["2026-09-25"], 6)
    with pytest.raises(ResearchError, match="不覆盖"):
        monthly_calendar("2026-09", ["2025-09-15"], 5)
    pool = [
        {"date": day, "contract": key, "product": "aa", "group": "commodity"}
        for day, key in [
            ("2026-09-01", "aa2610.SHFE"),
            ("2026-09-02", "aa2610.SHFE"),
            ("2026-09-03", "aa2611.SHFE"),
        ]
    ]
    requests = minute_plan(pool, calendar, 5)
    for key in ["aa2610.SHFE", "aa2611.SHFE"]:
        assert (
            sum(r["purpose"] == "warmup" and r["contract"] == key for r in requests)
            == 5
        )
    assert not any(r["trading_day"] == "2026-08-24" for r in requests)


def write_edb(path, rows):
    with Path(path).open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "datetime_nano",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_oi",
                "close_oi",
            ]
        )
        for dt, volume, op_oi, cl_oi in rows:
            writer.writerow(
                [
                    int(dt.timestamp()) * 1000000000,
                    100,
                    101,
                    99,
                    100,
                    volume,
                    op_oi,
                    cl_oi,
                ]
            )


def test_edb_day_boundary_and_open_oi_do_not_mix_night_and_day(tmp_path):
    calendar = monthly_calendar("2026-09", ["2026-09-25"], 5)
    calendar["profiles"]["test"] = [
        ["09:00", "10:15"],
        ["10:30", "11:30"],
        ["13:30", "15:00"],
    ]
    contract = {
        "exchange": "SHFE",
        "symbol": "aa2610",
        "product": "aa",
        "session_profile": "test",
    }
    path = tmp_path / "SYNTHETIC_TEST_ONLY_edb.csv"
    write_edb(path, [(at("2026-09-01", "00:00"), 100, 300, 350)])
    daily = normalize_daily(path, contract, calendar)
    assert daily[0]["trading_day"] == "2026-09-01"
    assert daily[0]["open_interest"] == 350 and daily[0]["complete"]
    write_edb(
        path,
        [
            (at("2026-09-01", "09:00"), 0, 300, 301),
            (at("2026-09-01", "09:01"), 10, 301, 302),
            (at("2026-09-01", "15:00"), 10, 302, 303),
            (at("2026-09-01", "21:00"), 1000, 600, 900),
        ],
    )
    minutes, removed = normalize_minutes(
        path, contract, calendar, [{"trading_day": "2026-09-01"}]
    )
    assert len(minutes) == 2 and removed["outside_requested_day_sessions"] == 2
    assert minutes[0]["session_open_oi"] == 300
    assert minutes[0]["volume"] == 0 and minutes[0]["open_interest"] == 301
    assert minutes[1]["session_open_oi"] is None and minutes[1]["turnover"] is None
    assert Calendar(calendar).bounds("2026-09-01", contract)[1] == at(
        "2026-09-01", "15:00"
    )


def test_config_blocker_preflight_cannot_generate_zero_profit(base, tmp_path):
    cfg = copy.deepcopy(base.cfg)
    cfg["research_blockers"] = ["SYNTHETIC_TEST_ONLY 演示阻塞：历史全合约目录未核实"]
    directory, result = run_one(base, cfg, tmp_path, cfg["splits"]["validation"])
    assert result["status"] == "failed"
    assert "全合约目录未核实" in result["error"]
    assert "metrics" not in result
    assert (directory / "report.md").exists()


def test_budget_scan_handles_disappearing_temporary_journal(tmp_path, monkeypatch):
    original = Path.stat
    journal = tmp_path / "temporary.db-journal"
    journal.write_bytes(b"x" * 10)
    (tmp_path / "preserved.csv").write_bytes(b"x" * 20)

    def vanished(path, *args, **kwargs):
        if path == journal:
            raise FileNotFoundError("SYNTHETIC_TEST_ONLY temporary journal disappeared")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", vanished)
    assert directory_bytes([tmp_path]) == 20


def test_public_response_caps_and_disk_stop_never_publish_complete_file(tmp_path):
    import httpx

    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": 100000, "min_free_bytes": 0}
    )
    client = PublicDownloader(tmp_path, budget)
    client.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"SYNTHETIC_TEST_ONLY" * 100)
        )
    )
    url = edb_url("SHFE.rb2701", 60, "2026-09-01 09:00:00", "2026-09-01 15:00:00")
    try:
        with pytest.raises(ResearchError, match="大小上限"):
            client.fetch(url, "over_cap.csv", cap=10)
        assert not (tmp_path / "over_cap.csv").exists()
        assert not (tmp_path / "over_cap.csv.receipt.json").exists()
        client.budget.policy["max_bytes"] = directory_bytes([tmp_path]) + 100
        with pytest.raises(ResearchError, match="空间预算"):
            client.fetch(url, "over_disk.csv")
        assert not (tmp_path / "over_disk.csv").exists()
    finally:
        client.http.close()


def test_bridge_daily_close_updates_are_causal(base):
    from research.vnpy_adapter import CompletedBarBridge

    data = Dataset(base.bars, base.cfg, daily=observations(base))
    bridge = CompletedBarBridge(data, at("2026-01-08", "09:15"))
    today = [r for r in data.daily if r.trading_day == "2026-01-08"]
    with pytest.raises(ResearchError, match="日线未完成"):
        bridge.completed_daily(today, at("2026-01-08", "14:59"))
    bridge.completed_daily(today, at("2026-01-08", "15:00"))
    assert bridge.data.pool("2026-01-09") == data.pool("2026-01-09")


def test_resume_preserves_user_fee_and_strategy_changes(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"SYNTHETIC_TEST_ONLY": True, "fees": []}))
    progress = {"generated_config_sha256": {"metadata.json": file_sha256(path)}}
    protect_generated_config(tmp_path, progress)
    edited = {"SYNTHETIC_TEST_ONLY": True, "fees": [{"open": 1, "close_today": 2}]}
    path.write_text(json.dumps(edited))
    with pytest.raises(ResearchError, match="保留参数"):
        protect_generated_config(tmp_path, progress)
    assert json.loads(path.read_text()) == edited
