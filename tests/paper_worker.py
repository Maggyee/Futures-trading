"""Browser-only gateway fixture. Uses temporary runtime and never connects CTP."""

import signal
from datetime import datetime
from threading import Event as ThreadEvent

from backend.config import prepare_runtime

prepare_runtime()

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, Product, Status
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.object import (
    AccountData,
    ContractData,
    PositionData,
    TickData,
    TradeData,
)

from backend.gateway import SYNC_EVENT
from backend.market import TZ
from backend.worker import Worker


class PaperWorker(Worker):
    def __init__(self):
        super().__init__()
        self.volumes = {Direction.LONG: 0, Direction.SHORT: 0}
        self.order_number = 0
        self.main.send_order = self.fill
        self.gateway.query_position = self.query
        self.main.subscribe = lambda *args: None
        self.events.register(EVENT_TIMER, self.market_tick)

    def query(self):
        for direction, volume in self.volumes.items():
            self.gateway.on_position(
                PositionData(
                    symbol="rbTEST",
                    exchange=Exchange.SHFE,
                    direction=direction,
                    volume=volume,
                    gateway_name="CTP",
                )
            )
        self.events.put(Event(SYNC_EVENT, {"kind": "positions", "ok": True}))

    def market_tick(self, event=None):
        if not self.connections_started:
            return
        self.query()
        self.gateway.on_tick(
            TickData(
                symbol="rbTEST",
                exchange=Exchange.SHFE,
                name="E2E 合成行情",
                datetime=datetime.now(TZ),
                last_price=3500,
                bid_price_1=3499,
                ask_price_1=3501,
                bid_volume_1=10,
                ask_volume_1=10,
                volume=1000,
                limit_up=4000,
                limit_down=3000,
                gateway_name="CTP",
            )
        )

    def fill(self, req, gateway):
        self.order_number += 1
        order = req.create_order_data(str(self.order_number), gateway)
        direction = (
            req.direction
            if req.offset == Offset.OPEN
            else (
                Direction.SHORT if req.direction == Direction.LONG else Direction.LONG
            )
        )
        rejected = req.offset != Offset.OPEN and self.volumes[direction] < req.volume
        order.status = Status.REJECTED if rejected else Status.ALLTRADED
        order.traded = 0 if rejected else req.volume
        self.gateway.on_order(order)
        if not rejected:
            self.volumes[direction] += (
                req.volume if req.offset == Offset.OPEN else -req.volume
            )
            self.gateway.on_trade(
                TradeData(
                    symbol=req.symbol,
                    exchange=req.exchange,
                    orderid=order.orderid,
                    tradeid=order.orderid,
                    direction=req.direction,
                    offset=req.offset,
                    price=req.price,
                    volume=req.volume,
                    datetime=datetime.now(TZ),
                    gateway_name=gateway,
                )
            )
            self.query()
        return order.vt_orderid

    def execute(self, action, payload, key):
        if action == "connect":
            self.connections_started = True
            self.gateway.td_api.login_status = self.gateway.md_api.login_status = True
            self.gateway.td_api.contract_inited = True
            self.gateway.on_contract(
                ContractData(
                    symbol="rbTEST",
                    exchange=Exchange.SHFE,
                    name="E2E 合成合约",
                    product=Product.FUTURES,
                    size=10,
                    pricetick=1,
                    min_volume=1,
                    max_volume=10,
                    gateway_name="CTP",
                )
            )
            self.gateway.on_account(
                AccountData(accountid="E2E ONLY", balance=1000000, gateway_name="CTP")
            )
            self.market_tick()
            return {"message": "测试柜台已连接，全部回报均为测试数据"}
        return super().execute(action, payload, key)


if __name__ == "__main__":
    stopped = ThreadEvent()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    worker = PaperWorker()
    worker.start()
    try:
        stopped.wait()
    finally:
        worker.close()
