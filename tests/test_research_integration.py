import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.auth import HASHER
from backend.config import ORIGIN
from backend.store import Store
from research.calendar import at
from research.config import ResearchError, digest, read_config
from research.data import load_data
from research.experiments import check_frozen, code_identity, freeze_run
from research.fixtures import create_fixture
from research.vnpy_adapter import CompletedBarBridge, portfolio_strategy_class, to_vnpy


def test_vnpy_bridge_parity_no_orders_and_completion_guard(tmp_path):
    cfg = create_fixture(tmp_path / "SYNTHETIC_TEST_ONLY")
    data = load_data(cfg)
    day = "2026-01-08"
    bridge = CompletedBarBridge(data, at(day, "09:15"))
    # Native callbacks are verified against the installed StrategyTemplate, with routing disabled.
    strategy = portfolio_strategy_class()(
        object(), "offline", list(data.by_contract), {}
    )
    strategy.on_init()
    strategy.bind(bridge)
    strategy.observed_at = at(day, "09:16")
    bar = data.by_day[(day, "aa2603.SHFE")][at(day, "09:15")]
    strategy.on_bars({bar.key: to_vnpy(bar)})
    assert strategy.audit_records
    row = strategy.audit_records[0]
    from research.signals import Features, SignalLogic, rank_candidates

    pool, _ = data.pool(day)
    candidates, _ = rank_candidates(data, day, pool, at(day, "09:08"), 1)
    candidate = next(r for r in candidates if r["contract"] == bar.key)
    expected = SignalLogic(data, Features(data)).evaluate(bar, candidate)
    assert row["filters"] == expected["filters"]
    assert row["snapshot"] == expected["snapshot"]
    with pytest.raises(ResearchError, match="禁止委托"):
        strategy.buy(bar.key, 100, 1)
    with pytest.raises(ResearchError, match="未完成"):
        bridge.completed(
            [data.by_day[(day, bar.key)][at(day, "09:17")]], at(day, "09:17")
        )


def test_freeze_snapshots_are_readable_and_changed_strategy_rejected(tmp_path):
    cfg = create_fixture(tmp_path / "fixture")
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "configuration": cfg,
        "code_hash": code_identity()["code_hash"],
        "split": "validation",
        "experiment_id": "run_fixture",
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "result.json").write_text(json.dumps({"status": "completed"}))
    (run / "data_quality.json").write_text(json.dumps({"sources": []}))
    (run / "config_snapshot.json").write_text(json.dumps(cfg))
    frozen = tmp_path / "frozen.json"
    freeze_run(run, frozen)
    restored = read_config(run / "config_snapshot.json")
    assert digest(restored) == digest(cfg)
    check_frozen(frozen, restored)
    restored["strategy"]["k"] = 2
    with pytest.raises(ResearchError, match="冻结方案"):
        check_frozen(frozen, restored)
    manifest["split"] = "test"
    (run / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ResearchError, match="锁定测试"):
        freeze_run(run, frozen)


def test_read_only_database_adapter(tmp_path):
    cfg = create_fixture(tmp_path / "fixture")
    path = tmp_path / "bars.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE dbbardata (symbol,exchange,datetime,interval,volume,turnover,open_interest,open_price,high_price,low_price,close_price)"
        )
        db.execute(
            "INSERT INTO dbbardata VALUES ('aa2603','SHFE','2026-01-08 09:00:00','1m',100,100000,10000,100,101,99,100)"
        )
    before = path.read_bytes()
    cfg["data"]["sources"] = [
        {
            "path": str(path),
            "format": "vnpy_sqlite",
            "provenance": "SYNTHETIC_TEST_ONLY",
        }
    ]
    data = load_data(cfg)
    assert len(data.bars) == 1 and data.bars[0].datetime == at("2026-01-08", "09:00")
    assert data.bars[0].turnover == 100000
    assert path.read_bytes() == before


def test_locked_prices_are_not_parsed_during_training(tmp_path):
    import csv

    import pandas as pd

    from research.experiments import calibrate_ticks

    cfg = create_fixture(tmp_path / "fixture")
    clean = load_data(cfg, cutoff=cfg["splits"]["train"]["end"])
    expected = calibrate_ticks(clean, cfg)
    source = Path(cfg["data"]["sources"][0]["path"])
    rows = list(csv.DictReader(source.open()))
    for row in rows:
        if row["trading_day"] == cfg["splits"]["test"]["start"]:
            row["open"] = "INVALID_LOCKED_PRICE_MUST_NOT_BE_PARSED"
    with source.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    full = load_data(cfg)
    assert full.quality["errors"]
    training = load_data(cfg, cutoff=cfg["splits"]["train"]["end"])
    assert not training.quality["errors"]
    assert calibrate_ticks(training, cfg) == expected
    assert not full.until(cfg["splits"]["train"]["end"]).quality["errors"]
    path = tmp_path / "locked.parquet"
    pd.DataFrame(rows).to_parquet(path)
    cfg["data"]["sources"] = [{"format": "parquet", "path": str(path)}]
    training = load_data(cfg, cutoff=cfg["splits"]["train"]["end"])
    assert not training.quality["errors"]
    assert calibrate_ticks(training, cfg) == expected


def test_parquet_and_normalized_csv_roundtrip(tmp_path):
    import pandas as pd

    from research.data import export_csv

    cfg = create_fixture(tmp_path / "fixture")
    data = load_data(cfg)
    path = tmp_path / "bars.parquet"
    pd.DataFrame([b.wire() for b in data.bars]).to_parquet(path, index=False)
    cfg["data"]["sources"] = [{"path": str(path), "format": "parquet"}]
    assert load_data(cfg).fingerprint == data.fingerprint
    path = tmp_path / "normalized.csv.gz"
    export_csv(data, path)
    cfg["data"]["sources"] = [{"path": str(path), "format": "csv"}]
    assert load_data(cfg).fingerprint == data.fingerprint


def test_research_api_authenticated_readonly_and_path_confined(tmp_path, monkeypatch):
    root = tmp_path / "research_outputs"
    run = root / "experiment" / "run_test"
    run.mkdir(parents=True)
    cfg = create_fixture(tmp_path / "fixture")
    manifest = {
        "experiment_id": "run_test",
        "configuration": cfg,
        "created_utc": "2026-01-01T00:00:00Z",
        "split": "validation",
        "scope": "shared",
        "window": cfg["splits"]["validation"],
        "data_coverage": [],
        "code_hash": "code",
        "data_fingerprint": "data",
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "summary.json").write_text(
        json.dumps({"status": "completed", "metrics": {"trade_count": 0}})
    )
    (run / "report.md").write_text("SYNTHETIC_TEST_ONLY")
    monkeypatch.setenv("WORKBENCH_RESEARCH_ROOT", str(root))
    store = Store(tmp_path / "auth.db")
    store.put(
        "config",
        "admin",
        {"username": "admin", "password_hash": HASHER.hash("test-password-123")},
    )
    calls = []
    app = create_app(store=store, rpc=lambda *args: calls.append(args))
    with TestClient(app) as c:
        assert c.get("/api/v1/research/runs").status_code == 401
        assert (
            c.post(
                "/api/v1/login",
                json={"username": "admin", "password": "test-password-123"},
                headers={"origin": ORIGIN},
            ).status_code
            == 200
        )
        assert c.get("/api/v1/research/runs").json()[0]["synthetic"]
        assert (
            c.get(
                "/api/v1/research/run", params={"path": "experiment/run_test"}
            ).json()["result"]["metrics"]["trade_count"]
            == 0
        )
        assert (
            c.get(
                "/api/v1/research/artifact",
                params={"path": "experiment/run_test", "name": "report.md"},
            ).text
            == "SYNTHETIC_TEST_ONLY"
        )
        assert (
            c.get("/api/v1/research/run", params={"path": "../fixture"}).status_code
            == 404
        )
        assert (
            c.get(
                "/api/v1/research/artifact",
                params={"path": "experiment/run_test", "name": "../../auth.db"},
            ).status_code
            == 404
        )
        assert not calls
