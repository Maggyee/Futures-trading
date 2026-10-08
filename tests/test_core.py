import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, Product
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.object import ContractData, PositionData, TickData

from backend.config import ORIGIN, RUNTIME
from backend.market import TZ, MinuteRecorder, aggregate, indicators, parse_csv
from backend.store import Store
from backend.strategies import TEMPLATES, load_class, save_version, validate_source
from backend.trading import order_request


def contract(exchange=Exchange.SHFE):
    return ContractData(
        symbol="rb2610",
        exchange=exchange,
        name="螺纹",
        product=Product.FUTURES,
        size=10,
        pricetick=1,
        min_volume=1,
        max_volume=100,
        gateway_name="CTP",
    )


def tick(dt, price=3500, volume=100):
    return TickData(
        symbol="rb2610",
        exchange=Exchange.SHFE,
        datetime=dt,
        last_price=price,
        volume=volume,
        bid_price_1=3499,
        ask_price_1=3501,
        limit_up=4000,
        limit_down=3000,
        gateway_name="CTP",
    )


class FakeMain:
    def __init__(self, exchange=Exchange.SHFE):
        self.contract = contract(exchange)
        self.tick = tick(datetime.now(TZ))
        self.converter = OffsetConverter(self)

    def get_contract(self, symbol):
        return self.contract if symbol == self.contract.vt_symbol else None

    def get_tick(self, symbol):
        return self.tick

    def get_converter(self, name):
        return self.converter


@pytest.mark.parametrize("exchange", [Exchange.SHFE, Exchange.INE])
def test_close_splits_today_and_yesterday_without_opening(exchange):
    main = FakeMain(exchange)
    position = PositionData(
        symbol="rb2610",
        exchange=exchange,
        direction=Direction.LONG,
        volume=5,
        yd_volume=3,
        gateway_name="CTP",
    )
    main.converter.update_position(position)
    p = {
        "symbol": main.contract.vt_symbol,
        "direction": "LONG",
        "volume": 4,
        "price": None,
    }
    req = order_request(main, p, "test", closing=True)
    result = main.converter.convert_order_request(req, lock=False, net=False)
    assert [(r.offset, r.volume) for r in result] == [
        (Offset.CLOSETODAY, 2),
        (Offset.CLOSEYESTERDAY, 2),
    ]
    assert all(r.direction == Direction.SHORT and r.price == 3499 for r in result)
    for i, r in enumerate(result):
        main.converter.update_order_request(r, f"CTP.{i}")
    with pytest.raises(ValueError, match="可平仓量不足"):
        order_request(main, {**p, "volume": 2}, "test", closing=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"price": 3500.5},
        {"price": float("nan")},
        {"price": 4100},
        {"volume": 0},
        {"volume": 1.5},
        {"volume": True},
    ],
)
def test_order_validation(payload):
    with pytest.raises(ValueError):
        order_request(
            FakeMain(),
            {
                "symbol": "rb2610.SHFE",
                "direction": "LONG",
                "volume": 1,
                "price": 3500,
                **payload,
            },
            "test",
        )


def test_record_midnight_gap_reset_and_out_of_order():
    bars = []
    recorder = MinuteRecorder(bars.append)
    dt = datetime(2026, 9, 28, 23, 59, 10, tzinfo=TZ)
    recorder.update(tick(dt, volume=100))
    recorder.update(tick(dt + timedelta(seconds=20), price=3502, volume=110))
    recorder.update(tick(dt + timedelta(minutes=1), price=3501, volume=115))
    assert bars[0].volume == 10
    assert bars[0].high_price == 3502
    recorder.update(tick(dt, price=1, volume=999))  # older tick cannot overwrite
    recorder.expire(dt + timedelta(minutes=3))
    assert bars[1].datetime.day == 29 and bars[1].volume == 5
    recorder.update(tick(dt + timedelta(hours=9), volume=2))
    recorder.expire(dt + timedelta(hours=9, minutes=2))
    assert len(bars) == 3 and bars[-1].volume == 0


CSV = "symbol,exchange,datetime,open,high,low,close,volume\nrb2610,SHFE,2026-09-28T21:00:00,3500,3502,3499,3501,10\n"


def test_csv_duplicate_and_validation():
    bars = parse_csv(CSV + CSV.splitlines()[1] + "\n")
    assert len(bars) == 1 and bars[0].datetime.utcoffset() == timedelta(hours=8)
    with pytest.raises(ValueError, match="第 2 行"):
        parse_csv(CSV.replace("3502", "3498"))
    with pytest.raises(ValueError):
        parse_csv(CSV.replace("3500", "NaN"))


def test_aggregation_does_not_fill_breaks_and_indicators_warmup():
    bars = parse_csv(
        CSV
        + CSV.splitlines()[1].replace("21:00", "21:01")
        + "\n"
        + CSV.splitlines()[1].replace("21:00", "23:00")
        + "\n"
    )
    merged = aggregate(bars, 5)
    assert len(merged) == 2 and merged[0].volume == 20
    assert indicators(merged)["MA"] == [None, None]


def test_csv_storage_updates_each_contract_overview_without_mutating_bars():
    from vnpy.trader.database import get_database

    from backend.market import save_bars

    rows = parse_csv(CSV + CSV.splitlines()[1].replace("rb2610", "ag2612") + "\n")
    save_bars(rows)
    save_bars(rows)
    assert all(isinstance(row.exchange, Exchange) and row.vt_symbol for row in rows)
    overview = {o.symbol: o.count for o in get_database().get_bar_overview()}
    assert overview["ag2612"] == 1


def test_idempotency_and_crash_recovery(tmp_path):
    s = Store(tmp_path / "db")
    fresh, _ = s.claim("x", {"volume": 1})
    assert fresh
    assert not s.claim("x", {"volume": 1})[0]
    with pytest.raises(ValueError):
        s.claim("x", {"volume": 2})
    s.finish("x", {"state": "submitting", "orders": ["CTP.1"]})
    s.recover()
    assert s.command("x")["state"] == "unknown" and s.command("x")["orders"] == [
        "CTP.1"
    ]


def test_strategy_versions_are_pinned_and_integrity_checked(tmp_path):
    store = Store(tmp_path / "db")
    first = save_version(store, "均线", TEMPLATES["双均线"])
    second = save_version(
        store,
        "均线",
        TEMPLATES["双均线"].replace("fast_window = 10", "fast_window = 11"),
    )
    assert load_class(first).fast_window == 10 and load_class(second).fast_window == 11
    (RUNTIME / "versions" / (first["id"] + ".py")).write_text("broken")
    with pytest.raises(ValueError, match="哈希"):
        load_class(first)
    with pytest.raises(ValueError, match="第 1 行"):
        validate_source("this is not python !")


@pytest.fixture
def client(tmp_path):
    from backend.api import create_app
    from backend.auth import HASHER

    store = Store(tmp_path / "auth.db")
    store.put(
        "config",
        "admin",
        {"username": "admin", "password_hash": HASHER.hash("test-password-123")},
    )
    app = create_app(
        store=store, rpc=lambda *args: {"environment": "SimNow", "ready": False}
    )
    with TestClient(app) as c:
        yield c, store


def login(client):
    response = client.post(
        "/api/v1/login",
        json={"username": "admin", "password": "test-password-123"},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 200
    return {
        "origin": ORIGIN,
        "x-csrf-token": response.json()["csrf"],
        "idempotency-key": str(uuid.uuid4()),
    }


def test_auth_csrf_and_logout(client):
    c, store = client
    assert c.get("/api/v1/snapshot").status_code == 401
    assert (
        c.post(
            "/api/v1/login", json={"username": "admin", "password": "test-password-123"}
        ).status_code
        == 403
    )
    headers = login(c)
    assert c.get("/api/v1/snapshot").status_code == 200
    assert c.post("/api/v1/connect").status_code == 403
    assert (
        c.post(
            "/api/v1/connect", headers={**headers, "origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert c.post("/api/v1/connect", headers=headers).status_code == 200
    assert c.post("/api/v1/logout", headers=headers).status_code == 200
    assert c.get("/api/v1/snapshot").status_code == 401


def test_expired_session_and_websocket_rejected(client):
    from starlette.websockets import WebSocketDisconnect

    c, store = client
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/v1/ws", headers={"origin": ORIGIN}):
            pass
    login(c)
    with store.db() as db:
        db.execute("UPDATE sessions SET expires=0")
    assert c.get("/api/v1/session").status_code == 401


def test_api_invalid_volume_does_not_reach_worker(client):
    c, _ = client
    headers = login(c)
    assert (
        c.post(
            "/api/v1/close",
            headers=headers,
            json={"symbol": "rb2610.SHFE", "direction": "LONG", "volume": 1.5},
        ).status_code
        == 422
    )


def test_query_interval_parses_url_string_as_integer(client):
    c, _ = client
    login(c)
    assert c.get("/api/v1/bars/rb2610.SHFE?minutes=1").status_code == 200
    assert c.get("/api/v1/bars/rb2610.SHFE?minutes=2").status_code == 400


def test_login_throttle(client):
    c, _ = client
    for _ in range(5):
        assert (
            c.post(
                "/api/v1/login",
                headers={"origin": ORIGIN},
                json={"username": "admin", "password": "wrong"},
            ).status_code
            == 401
        )
    r = c.post(
        "/api/v1/login",
        headers={"origin": ORIGIN},
        json={"username": "admin", "password": "test-password-123"},
    )
    assert "5 分钟" in r.json()["detail"]


def test_empty_query_emits_zero_and_completion():
    from vnpy.event import EventEngine

    from backend.gateway import SYNC_EVENT, SimNowGateway, symbol_contract_map

    c = contract()
    symbol_contract_map[c.symbol] = c
    seen = []
    gateway = SimNowGateway(EventEngine())
    gateway.on_position = seen.append
    gateway.event_engine.put = seen.append
    gateway.td_api.known = {(c.symbol, Direction.LONG)}
    gateway.td_api.onRspQryInvestorPosition({}, {}, 1, True)
    assert seen[0].volume == 0
    assert seen[-1].type == SYNC_EVENT and seen[-1].data["ok"]


def test_worker_serialized_duplicate_order_and_mismatched_retry(tmp_path):
    from backend.gateway import SYNC_EVENT
    from backend.worker import Worker

    w = Worker()
    try:
        w.store = Store(tmp_path / "worker.db")
        w.gateway.td_api.login_status = w.gateway.md_api.login_status = True
        w.gateway.td_api.contract_inited = True
        w.gateway.on_contract(contract())
        w.gateway.on_tick(tick(datetime.now(TZ)))
        w.events.put(Event(SYNC_EVENT, {"kind": "positions", "ok": True}))
        assert w.call("snapshot")["ready"]
        sent = []

        def send(req, gateway):
            sent.append(req)
            order = req.create_order_data(str(len(sent)), gateway)
            w.gateway.on_order(order)
            return order.vt_orderid

        w.main.send_order = send
        key = str(uuid.uuid4())
        payload = {
            "symbol": "rb2610.SHFE",
            "direction": "LONG",
            "volume": 1,
            "price": 3500,
        }
        first = w.call("order", payload, key)
        assert first["state"] == "submitted"
        assert w.call("order", payload, key) == first
        assert w.call("order", {**payload, "volume": 2}, key)["state"] == "failed"
        assert w.store.command(key) == first
        assert len(sent) == 1
    finally:
        w.close()
