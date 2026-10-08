"""Expose CTP query completion without changing the upstream gateway."""

from vnpy.event import Event
from vnpy.trader.object import PositionData
from vnpy_ctp.gateway.ctp_gateway import (
    DIRECTION_CTP2VT,
    CtpGateway,
    CtpMdApi,
    CtpTdApi,
    symbol_contract_map,
)

SYNC_EVENT = "workbench.sync"


class TrackedTdApi(CtpTdApi):
    def __init__(self, gateway):
        super().__init__(gateway)
        self.seen = set()
        self.known = set()
        self.query_error = False

    def onFrontDisconnected(self, reason):
        super().onFrontDisconnected(reason)
        self.contract_inited = False
        self.seen.clear()
        self.positions.clear()
        self.gateway.event_engine.put(Event(SYNC_EVENT, {"kind": "disconnected"}))

    def onRspQryInvestorPosition(self, data, error, reqid, last):
        if error.get("ErrorID"):
            self.query_error = True
        if data and data.get("InstrumentID") in symbol_contract_map:
            self.seen.add(
                (data["InstrumentID"], DIRECTION_CTP2VT[data["PosiDirection"]])
            )
        super().onRspQryInvestorPosition(data, error, reqid, last)
        if last:
            # Upstream returns early for an empty final packet; complete that query here.
            for position in self.positions.values():
                self.gateway.on_position(position)
            self.positions.clear()
            if not self.query_error:
                for symbol, direction in self.known - self.seen:
                    contract = symbol_contract_map[symbol]
                    self.gateway.on_position(
                        PositionData(
                            symbol=symbol,
                            exchange=contract.exchange,
                            direction=direction,
                            gateway_name=self.gateway_name,
                        )
                    )
                self.known = self.seen.copy()
            self.gateway.event_engine.put(
                Event(SYNC_EVENT, {"kind": "positions", "ok": not self.query_error})
            )
            self.seen.clear()
            self.query_error = False


class TrackedMdApi(CtpMdApi):
    def onFrontDisconnected(self, reason):
        super().onFrontDisconnected(reason)
        self.gateway.event_engine.put(Event(SYNC_EVENT, {"kind": "disconnected"}))


class SimNowGateway(CtpGateway):
    default_name = "CTP"

    def __init__(self, event_engine, gateway_name="CTP"):
        super().__init__(event_engine, gateway_name)
        self.td_api = TrackedTdApi(self)
        self.md_api = TrackedMdApi(self)
