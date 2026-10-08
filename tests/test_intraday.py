from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.market import TZ, intraday_points


def bar(hour=9, minute=0, **kwargs):
    return SimpleNamespace(
        datetime=datetime(2026, 9, 29, hour, minute, tzinfo=TZ),
        **{
            "open_price": 100,
            "close_price": 101,
            "volume": 2,
            "turnover": 2000,
            **kwargs,
        },
    )


def test_intraday_vwap_uses_turnover_multiplier_and_keeps_gaps():
    rows = [bar(), bar(hour=10, volume=1, turnover=1300, close_price=130)]
    result = intraday_points(rows, size=10)
    assert [p["average"] for p in result["bars"]] == [100, 110]
    assert [p["volume"] for p in result["bars"]] == [2, 1]
    assert len(result["bars"]) == 2 and not result["estimated"]


def test_intraday_missing_turnover_is_explicitly_estimated_and_zero_volume_not_weighted():
    result = intraday_points(
        [
            bar(volume=0, turnover=0),
            bar(minute=1, turnover=0, close_price=120),
            bar(minute=2, volume=1, turnover=0, close_price=150),
        ],
        size=10,
    )
    assert [p["average"] for p in result["bars"]] == [None, 120, 130]
    assert result["estimated"]
    assert intraday_points([bar()], size=0)["estimated"]
    assert intraday_points([])["bars"] == []


def test_intraday_day_boundary_and_live_bar_replace_database_copy(
    monkeypatch, tmp_path
):
    from backend import worker as module

    current = bar(minute=1, close_price=125)
    previous_day = bar()
    previous_day.datetime -= timedelta(days=1)
    next_day = bar(hour=0)
    next_day.datetime += timedelta(days=1)
    monkeypatch.setattr(
        module,
        "load_bars",
        lambda *args: [previous_day, bar(), bar(minute=1), next_day],
    )
    worker = module.Worker.__new__(module.Worker)
    from backend.store import Store

    worker.store = Store(tmp_path / "worker.db")
    worker.recorder = SimpleNamespace(current={"rb2610.SHFE": current})
    worker.main = SimpleNamespace(
        get_contract=lambda symbol: SimpleNamespace(size=10), get_all_trades=lambda: []
    )
    result = worker.execute(
        "intraday", {"symbol": "rb2610.SHFE", "date": "2026-09-29"}, None
    )
    assert result["date"] == "2026-09-29"
    assert len(result["bars"]) == 2
    assert result["bars"][-1]["close"] == 125


@pytest.mark.parametrize(
    "direction, closing, expected",
    [
        ("LONG", False, 102),
        ("SHORT", False, 99),
        ("LONG", True, 99),
        ("SHORT", True, 102),
    ],
)
def test_counterparty_order_side_and_missing_quote(direction, closing, expected):
    from test_core import FakeMain
    from vnpy.trader.constant import Direction, Exchange
    from vnpy.trader.object import PositionData

    from backend.trading import order_request

    main = FakeMain()
    main.tick.ask_price_1, main.tick.bid_price_1 = 102, 99
    main.tick.limit_down = 0
    if closing:
        main.converter.update_position(
            PositionData(
                symbol="rb2610",
                exchange=Exchange.SHFE,
                direction=Direction[direction],
                volume=1,
                gateway_name="CTP",
            )
        )
    payload = {
        "symbol": "rb2610.SHFE",
        "direction": direction,
        "volume": 1,
        "price": None,
    }
    assert order_request(main, payload, "test", closing).price == expected
    main.tick.ask_price_1 = main.tick.bid_price_1 = 0
    with pytest.raises(ValueError, match="没有对手报价"):
        order_request(main, payload, "test", closing)
