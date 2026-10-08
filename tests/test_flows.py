import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_core import FakeMain, contract, tick
from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Status
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData, OrderRequest, OrderType, PositionData, TradeData

from backend.config import ROOT, RUNTIME
from backend.market import TZ
from backend.store import Store
from backend.strategies import TEMPLATES, save_version


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


@pytest.mark.parametrize("reason", ["disconnect", "stale_tick", "timeout"])
def test_unsubmitted_close_does_not_resume_with_stale_intent(tmp_path, reason):
    from backend.worker import Worker

    # Exercise the state machine without native CTP connections or timer races.
    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "close.db")
    worker.cta = type("Cta", (), {"strategies": {}})()
    worker.reinit = set()
    worker.blocked = {"rb2610.SHFE"}
    worker.position_at = time.time()
    worker.last_trade_at = 0
    worker.last_tick_at = {"rb2610.SHFE": time.time() - 31}
    worker.ready = lambda: True
    worker.main = type("Main", (), {"get_all_active_orders": lambda self: []})()
    key = str(uuid.uuid4())
    payload = {"symbol": "rb2610.SHFE", "direction": "LONG", "volume": 1}
    worker.store.claim(key, {"action": "close", "payload": payload})
    worker.store.finish(key, {"state": "waiting_cancel", "orders": []})
    task = {
        "payload": payload,
        "started": time.time() - (31 if reason == "timeout" else 1),
        "stage": "cancel",
        "query_after": time.time() - 1,
        "orders": [],
    }
    worker.pending = {key: task}
    if reason == "disconnect":
        worker.on_sync(Event("sync", {"kind": "disconnected"}))
        worker.on_sync(Event("sync", {"kind": "positions", "ok": True}))
        assert "rb2610.SHFE" in worker.blocked
        assert worker.store.command(key)["state"] == "unknown"
    else:
        worker.advance_close(key, task)
        assert worker.store.command(key)["state"] == "failed"
        assert ("行情" if reason == "stale_tick" else "超时") in worker.store.command(
            key
        )["error"]
    assert key not in worker.pending
    assert worker.store.command(key)["orders"] == []


@pytest.mark.parametrize("action", ["order", "close"])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("price", [None, 3500])
def test_stale_quotes_block_counterparty_but_allow_explicit_limit(
    tmp_path, action, direction, price
):
    from backend.worker import Worker

    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "limits.db")
    worker.cta = SimpleNamespace(strategies={}, symbol_strategy_map={})
    worker.main = FakeMain()
    worker.main.get_all_active_orders = lambda: []
    worker.main.convert_order_request = lambda req, gateway, lock, net: (
        worker.main.converter.convert_order_request(req, lock=lock, net=net)
    )
    worker.main.converter.update_position(
        PositionData(
            symbol="rb2610",
            exchange=Exchange.SHFE,
            direction=Direction[direction],
            volume=1,
            gateway_name="CTP",
        )
    )
    worker.ready = lambda: True
    worker.last_tick_at = {"rb2610.SHFE": time.time() - 31}
    worker.blocked = set()
    worker.pending = {}
    worker.position_at = time.time()
    worker.last_trade_at = 0
    submitted = []

    def submit(key, requests):
        submitted.extend(requests)
        return {"orders": ["CTP.1"]}

    worker.submit = submit
    key = str(uuid.uuid4())
    payload = {
        "symbol": "rb2610.SHFE",
        "direction": direction,
        "volume": 1,
        "price": price,
    }
    worker.store.claim(key, {"action": action, "payload": payload})
    if price is None:
        with pytest.raises(ValueError, match="请填写限价"):
            worker.execute(action, payload, key)
        assert not submitted and not worker.pending
        return
    worker.execute(action, payload, key)
    if action == "close":
        task = worker.pending[key]
        assert not submitted
        task["query_after"] = time.time() - 1
        worker.advance_close(key, task)
        assert task["stage"] == "fills"
    assert len(submitted) == 1 and submitted[0].price == price
    requested_direction = Direction[direction]
    assert submitted[0].direction == (
        requested_direction
        if action == "order"
        else Direction.SHORT
        if direction == "LONG"
        else Direction.LONG
    )
    assert submitted[0].offset == (
        Offset.OPEN if action == "order" else Offset.CLOSETODAY
    )


@pytest.mark.parametrize("filled", [4, 2, 0])
def test_close_waits_for_cancel_then_splits_and_reconciles_strategy(tmp_path, filled):
    from backend.gateway import SYNC_EVENT
    from backend.worker import Worker

    worker = Worker()
    try:
        worker.store = Store(tmp_path / "worker.db")
        worker.gateway.td_api.login_status = worker.gateway.md_api.login_status = True
        worker.gateway.td_api.contract_inited = True
        worker.gateway.on_contract(contract())
        worker.gateway.on_tick(tick(datetime.now(TZ)))
        current = PositionData(
            symbol="rb2610",
            exchange=Exchange.SHFE,
            direction=Direction.LONG,
            volume=5,
            yd_volume=3,
            gateway_name="CTP",
        )

        def query():
            worker.gateway.on_position(current)
            worker.events.put(Event(SYNC_EVENT, {"kind": "positions", "ok": True}))

        worker.gateway.query_position = query
        query()
        worker.call("snapshot")
        version = save_version(worker.store, "CloseTest", TEMPLATES["双均线"])
        version["published"] = True
        worker.store.put("versions", version["id"], version)
        doc = {
            "name": "close_test",
            "version": version["id"],
            "symbol": "rb2610.SHFE",
            "parameters": {},
        }
        worker.restore_instance(doc)
        worker.store.put("instances", doc["name"], doc)
        strategy = worker.cta.strategies[doc["name"]]
        strategy.pos = 5
        strategy.inited = strategy.trading = True
        resting = OrderRequest(
            symbol="rb2610",
            exchange=Exchange.SHFE,
            direction=Direction.LONG,
            type=OrderType.LIMIT,
            volume=1,
            price=3400,
            offset=Offset.OPEN,
        ).create_order_data("old", "CTP")
        resting.status = Status.NOTTRADED
        worker.gateway.on_order(resting)
        cancelled = []
        worker.main.cancel_order = lambda req, gateway: cancelled.append(req.orderid)
        sent = []

        def send(req, gateway):
            order = req.create_order_data(str(len(sent) + 1), gateway)
            order.status = Status.NOTTRADED
            sent.append((req, order))
            worker.gateway.on_order(order)
            return order.vt_orderid

        worker.main.send_order = send
        worker.call("snapshot")
        key = str(uuid.uuid4())
        payload = {
            "symbol": "rb2610.SHFE",
            "direction": "LONG",
            "volume": 4,
            "price": 3500,
        }
        result = worker.call("close", payload, key)
        assert result["state"] == "waiting_cancel"
        assert not strategy.trading and "old" in cancelled and not sent
        resting.status = Status.CANCELLED
        worker.gateway.on_order(resting)
        wait_for(lambda: len(sent) == 2)
        assert [(req.offset, req.volume) for req, _ in sent] == [
            (Offset.CLOSETODAY, 2),
            (Offset.CLOSEYESTERDAY, 2),
        ]
        assert len(worker.call("close", payload, key)["orders"]) == 2
        current.volume = 5 - filled
        current.yd_volume = min(3, current.volume)
        remaining = filled
        for req, order in sent:
            amount = min(req.volume, remaining)
            remaining -= amount
            order.status = (
                Status.ALLTRADED
                if amount == req.volume
                else Status.CANCELLED
                if filled
                else Status.REJECTED
            )
            order.traded = amount
            worker.gateway.on_order(order)
            if not amount:
                continue
            worker.gateway.on_trade(
                TradeData(
                    symbol=req.symbol,
                    exchange=req.exchange,
                    orderid=order.orderid,
                    tradeid=order.orderid,
                    direction=req.direction,
                    offset=req.offset,
                    price=req.price,
                    volume=amount,
                    datetime=datetime.now(TZ),
                    gateway_name="CTP",
                )
            )
        wait_for(lambda: worker.store.command(key)["state"] == "completed")
        assert (
            strategy.pos == 5 - filled and not strategy.inited and not strategy.trading
        )
        assert doc["name"] in worker.reinit
        assert worker.store.command(key)["filled"] == filled
    finally:
        worker.close()


def test_real_backtest_subprocess_uses_pinned_strategy_and_history():
    store = Store()
    version = save_version(store, "回测测试", TEMPLATES["双均线"])
    version["published"] = True
    store.put("versions", version["id"], version)
    bars = []
    for day in (27, 28):
        for i in range(180):
            close = 3500 + math.sin(i / 7) * 30
            bars.append(
                BarData(
                    symbol="bt2610",
                    exchange=Exchange.SHFE,
                    datetime=datetime(2026, 9, day, 9, tzinfo=TZ)
                    + timedelta(minutes=i),
                    interval=Interval.MINUTE,
                    gateway_name="TEST",
                    open_price=close,
                    high_price=close + 10,
                    low_price=close - 10,
                    close_price=close,
                    volume=10,
                )
            )
    get_database().save_bar_data(bars)
    key = str(uuid.uuid4())
    params = {
        "version": version["id"],
        "symbol": "bt2610.SHFE",
        "settings": {"bar_minutes": 1},
        "start": "2026-09-28T00:00:00+08:00",
        "end": "2026-09-28T23:59:00+08:00",
        "rate": 0.0001,
        "slippage": 1,
        "size": 10,
        "pricetick": 1,
        "capital": 1000000,
    }
    store.put("backtests", key, {"id": key, "state": "queued", "parameters": params})
    result = subprocess.run(
        [sys.executable, "-m", "backend.backtest", key],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    job = store.get("backtests", key)
    assert job["state"] == "completed", job
    assert job["bar_count"] == 180 and job["warmup_count"] == 180
    assert len(job["chart"]["bars"]) == 180 and job["chart"]["minutes"] == 1
    assert job["trades"] and job["equity"] and job["code_hash"] == version["sha256"]
    assert (RUNTIME / (key + ".bars.json.gz")).exists()


def test_backtest_missing_warmup_is_failed_not_empty_success():
    store = Store()
    version = save_version(store, "无数据测试", TEMPLATES["布林带"])
    version["published"] = True
    store.put("versions", version["id"], version)
    key = str(uuid.uuid4())
    params = {
        "version": version["id"],
        "symbol": "missing.SHFE",
        "settings": {},
        "start": "2026-09-28",
        "end": "2026-09-29",
        "rate": 0.0001,
        "slippage": 1,
        "size": 10,
        "pricetick": 1,
        "capital": 1000000,
    }
    store.put("backtests", key, {"id": key, "state": "queued", "parameters": params})
    subprocess.run(
        [sys.executable, "-m", "backend.backtest", key],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        timeout=30,
    )
    job = store.get("backtests", key)
    assert job["state"] == "failed" and "没有一分钟历史数据" in job["error"]
