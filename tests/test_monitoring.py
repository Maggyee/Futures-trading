from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_core import contract
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from backend.market import TZ
from backend.store import Store
from backend.strategies import TEMPLATES, load_class, save_version, validate_parameters
from backend.worker import Worker


def test_watchlist_preserves_addition_order_removal_and_restart(tmp_path):
    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "watch.db")
    worker.subscriptions = set()
    first = contract()
    second = contract()
    second.symbol = "cu2611"
    second.vt_symbol = "cu2611.SHFE"
    calls = []
    worker.main = SimpleNamespace(
        get_contract=lambda s: {first.vt_symbol: first, second.vt_symbol: second}.get(
            s
        ),
        subscribe=lambda request, gateway: calls.append(request.symbol),
    )
    worker.execute("subscribe", {"symbol": first.vt_symbol}, None)
    worker.execute("subscribe", {"symbol": second.vt_symbol}, None)
    worker.execute("subscribe", {"symbol": first.vt_symbol}, None)
    assert worker.watchlist() == [first.vt_symbol, second.vt_symbol]
    worker.execute("watchlist_remove", {"symbol": first.vt_symbol}, None)
    assert worker.watchlist() == [second.vt_symbol]
    assert first.vt_symbol in worker.subscriptions
    worker.execute("subscribe", {"symbol": first.vt_symbol}, None)
    assert worker.watchlist() == [second.vt_symbol, first.vt_symbol]
    worker.subscriptions.clear()
    worker.store = Store(worker.store.path)
    assert worker.watchlist() == [second.vt_symbol, first.vt_symbol]
    worker.execute("watchlist_remove", {"symbol": first.vt_symbol}, None)
    worker.execute("watchlist_remove", {"symbol": second.vt_symbol}, None)
    worker.subscriptions.add(first.vt_symbol)
    assert worker.watchlist() == []


@pytest.mark.parametrize("template", ["双均线", "布林带"])
def test_strategy_chart_matches_instance_array_manager_and_owned_fills(
    tmp_path, template
):
    worker = Worker.__new__(Worker)
    worker.store = Store(tmp_path / "chart.db")
    version = save_version(worker.store, "监控验证", TEMPLATES[template])
    cls = load_class(version)
    params = validate_parameters(cls, {"bar_minutes": 1})
    strategy = cls(SimpleNamespace(), "monitor", "rb2610.SHFE", params)
    for i in range(120):
        strategy.on_bar(
            BarData(
                symbol="rb2610",
                exchange=Exchange.SHFE,
                interval=Interval.MINUTE,
                datetime=datetime(2026, 9, 29, 9, 0, tzinfo=TZ) + timedelta(minutes=i),
                open_price=3500 + i,
                high_price=3501 + i,
                low_price=3499 + i,
                close_price=3500 + i,
                volume=10,
                gateway_name="test",
            )
        )
    worker.store.put(
        "instances",
        "monitor",
        {
            "name": "monitor",
            "version": version["id"],
            "symbol": strategy.vt_symbol,
            "parameters": params,
        },
    )
    worker.cta = SimpleNamespace(strategies={"monitor": strategy})
    worker.initializing = {}
    worker.trade_history = lambda s: [
        {"strategy": "monitor", "version": version["id"], "price": 3500},
        {"strategy": "other", "version": version["id"]},
        {"strategy": "monitor", "version": "older-version"},
        {"price": 3500},
    ]
    result = worker.execute("strategy_chart", {"name": "monitor"}, None)
    assert len(result["bars"]) == 100
    assert result["bars"][0]["close"] == 3520
    assert result["bars"][-1]["close"] == 3619
    assert len(result["trades"]) == 1
    if template == "双均线":
        assert result["indicators"]["SMA10"][-1] == strategy.am.sma(10)
        assert result["indicators"]["SMA20"][-1] == strategy.am.sma(20)
    else:
        upper, lower = strategy.am.boll(20, 2)
        assert result["indicators"]["BOLL_UP"][-1] == upper
        assert result["indicators"]["BOLL_LOW"][-1] == lower
    worker.initializing["monitor"] = object()
    assert worker.execute("strategy_chart", {"name": "monitor"}, None)["bars"] == []


def test_strategy_monitor_keeps_pending_window_out_of_completed_bars(tmp_path):
    from backend.strategy_base import WorkbenchStrategy

    strategy = WorkbenchStrategy(SimpleNamespace(), "monitor", "rb2610.SHFE", {})
    for i in range(10):
        strategy.on_bar(
            BarData(
                symbol="rb2610",
                exchange=Exchange.SHFE,
                datetime=datetime(2026, 9, 29, 9, 0, tzinfo=TZ) + timedelta(minutes=i),
                open_price=3500,
                high_price=3500,
                low_price=3500,
                close_price=3500,
                volume=1,
                gateway_name="test",
            )
        )
    assert len(strategy.monitor_bars) == 1
    assert strategy.monitor_bars[0].datetime.minute == 0
    assert strategy.monitor_bars[0].volume == 5
