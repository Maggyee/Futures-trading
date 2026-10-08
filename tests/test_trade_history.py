from datetime import datetime, timedelta
from types import SimpleNamespace

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset
from vnpy.trader.object import TradeData

from backend.market import TZ
from backend.store import Store, wire
from backend.worker import Worker


def test_fills_persist_deduplicate_and_filter_by_contract_and_time(tmp_path):
    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "worker.db")
    worker.cta = SimpleNamespace(strategies={})
    at = datetime(2026, 9, 29, 22, 27, 47, tzinfo=TZ)
    trade = TradeData(
        symbol="rb2701",
        exchange=Exchange.SHFE,
        orderid="1",
        tradeid="1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        volume=1,
        price=3117,
        datetime=at,
        gateway_name="CTP",
    )
    worker.main = SimpleNamespace(get_all_trades=lambda: [trade])
    worker.on_trade(Event("trade", trade))
    worker.on_trade(Event("trade", trade))
    assert worker.trade_history() == [wire(trade)]
    worker.main.get_all_trades = lambda: []
    assert worker.trade_history() == [wire(trade)]
    assert worker.trade_history("au2612.SHFE") == []
    assert worker.trade_history("rb2701.SHFE", at, at + timedelta(seconds=1)) == [
        wire(trade)
    ]
    assert worker.trade_history("rb2701.SHFE", end=at) == []
    trade.datetime = at + timedelta(days=1)
    worker.on_trade(Event("trade", trade))
    assert len(worker.trade_history()) == 2


def test_live_trade_copy_preserves_persisted_strategy_ownership(tmp_path):
    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "worker.db")
    trade = TradeData(
        symbol="rb2701",
        exchange=Exchange.SHFE,
        orderid="1",
        tradeid="1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        volume=1,
        price=3117,
        datetime=datetime(2026, 9, 29, 22, 27, tzinfo=TZ),
        gateway_name="CTP",
    )
    worker.store.put("instances", "ma", {"name": "ma", "version": "v1"})
    owner = SimpleNamespace(strategy_name="ma", pos=1)
    worker.cta = SimpleNamespace(
        strategies={"ma": owner}, orderid_strategy_map={trade.vt_orderid: owner}
    )
    worker.main = SimpleNamespace(get_all_trades=lambda: [trade])
    worker.on_trade(Event("trade", trade))
    assert worker.trade_history()[0]["strategy"] == "ma"
    assert worker.trade_history()[0]["version"] == "v1"
