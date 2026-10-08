import signal
import time
import traceback
from concurrent.futures import Future
from datetime import datetime, timedelta
from threading import Event as ThreadEvent

from .config import RPC_PUBLISH, RPC_REQUEST, prepare_runtime, read_ctp_config
from .native_locale import prepare_ctp_locale

prepare_runtime()
prepare_ctp_locale()

from vnpy.event import Event, EventEngine
from vnpy.rpc import RpcServer
from vnpy.trader.constant import Direction
from vnpy.trader.database import get_database
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import (
    EVENT_LOG,
    EVENT_TICK,
    EVENT_TIMER,
    EVENT_TRADE,
)
from vnpy.trader.object import SubscribeRequest
from vnpy_ctastrategy.engine import CtaEngine

from .gateway import SYNC_EVENT, SimNowGateway
from .market import (
    TZ,
    MinuteRecorder,
    aggregate,
    indicators,
    intraday_points,
    load_bars,
    parse_csv,
    save_bars,
    stamp,
)
from .store import Store, wire
from .strategies import load_class, validate_parameters
from .trading import order_request

COMMAND_EVENT = "workbench.command"


class GuardedCtaEngine(CtaEngine):
    def __init__(self, *args):
        super().__init__(*args)
        self.guard = lambda strategy: False
        self.errors = {}

    def send_order(self, strategy, *args, **kwargs):
        if not self.guard(strategy):
            return []
        return super().send_order(strategy, *args, **kwargs)

    def send_server_order(self, strategy, *args, **kwargs):
        # Local stop-order triggers also pass here, bypassing send_order above.
        if not strategy.trading or not self.guard(strategy):
            return []
        return super().send_server_order(strategy, *args, **kwargs)

    def stop_strategy(self, name):
        strategy = self.strategies[name]
        was_trading = strategy.trading
        strategy.trading = False
        if was_trading:
            self.call_strategy_func(strategy, strategy.on_stop)
        self.cancel_all(strategy)
        self.sync_strategy_data(strategy)
        self.put_strategy_event(strategy)

    def call_strategy_func(self, strategy, func, params=None):
        try:
            if params is None:
                func()
            else:
                func(params)
        except Exception:
            strategy.trading = False
            strategy.inited = False
            self.errors[strategy.strategy_name] = traceback.format_exc()
            self.cancel_all(strategy)
            self.write_log(
                "策略执行异常，已停止：" + self.errors[strategy.strategy_name], strategy
            )


class Worker:
    def __init__(self):
        self.store = Store()
        self.store.recover()
        self.events = EventEngine()
        self.main = MainEngine(self.events)
        self.gateway = self.main.add_gateway(SimNowGateway)
        self.cta = self.main.add_engine(GuardedCtaEngine)
        self.cta.register_event()
        self.cta.guard = self.strategy_guard
        self.rpc = RpcServer()
        self.rpc.register(self.call)
        self.logs = []
        self.connections_started = False
        self.position_at = 0
        self.last_trade_at = 0
        self.pending = {}
        self.blocked = {
            d["symbol"]
            for d in self.store.commands(10000)
            if d.get("symbol") and d["state"] == "unknown"
        }
        self.initializing = {}
        self.reinit = {d["name"] for d in self.store.all("instances")}
        self.subscriptions = set()
        self.last_tick_at = {}
        self.recorder = MinuteRecorder(lambda bar: save_bars([bar]))
        self.events.register(COMMAND_EVENT, self.on_command)
        self.events.register(SYNC_EVENT, self.on_sync)
        self.events.register(EVENT_TIMER, self.on_timer)
        self.events.register(EVENT_TICK, self.on_tick)
        self.events.register(EVENT_TRADE, self.on_trade)
        self.events.register_general(self.publish)
        for doc in self.store.all("instances"):
            try:
                self.restore_instance(doc)
            except Exception as exc:
                self.logs.append({"msg": f"策略 {doc['name']} 恢复失败：{exc}"})

    def start(self):
        self.rpc.start(RPC_REQUEST, RPC_PUBLISH)

    def close(self):
        self.rpc.stop()
        self.rpc.join()
        self.rpc._socket_rep.close(linger=0)
        self.rpc._socket_pub.close(linger=0)
        self.rpc._context.term()
        self.main.close()
        self.cta.init_executor.shutdown(wait=True)

    def ready(self):
        return bool(
            self.gateway.td_api.login_status
            and self.gateway.md_api.login_status
            and self.gateway.td_api.contract_inited
            and self.position_at
            and time.time() - self.position_at < 60
        )

    def require_ready(self):
        if not self.ready():
            raise ValueError("等待行情、交易登录及持仓查询完成")

    def strategy_guard(self, strategy):
        return (
            self.ready()
            and strategy.vt_symbol not in self.blocked
            and strategy.strategy_name not in self.reinit
            and time.time() - self.last_tick_at.get(strategy.vt_symbol, 0) < 30
        )

    def call(self, action, payload=None, request_id=None):
        payload = payload or {}
        future = Future()
        self.events.put(Event(COMMAND_EVENT, (action, payload, request_id, future)))
        return future.result(timeout=15)

    def on_command(self, event):
        action, payload, key, future = event.data
        mutating = action not in {
            "snapshot",
            "bars",
            "intraday",
            "strategy_chart",
            "command",
            "overview",
        }
        claimed = False
        try:
            if mutating:
                if not key or len(key) > 80:
                    raise ValueError("写操作需要唯一请求标识")
                fresh, result = self.store.claim(
                    key, {"action": action, "payload": payload}
                )
                if not fresh:
                    future.set_result(result)
                    return
                claimed = True
                self.store.audit(
                    action,
                    {
                        "id": key,
                        "payload": payload if action != "import" else {"csv": "省略"},
                    },
                )
            result = self.execute(action, payload, key)
            if mutating and key not in self.pending:
                result = self.store.finish(
                    key,
                    {
                        "state": "submitted" if action == "order" else "completed",
                        "result": result,
                        **(
                            {"orders": result["orders"], "requested": payload["volume"]}
                            if action == "order"
                            else {}
                        ),
                    },
                )
            elif mutating:
                result = self.store.command(key)
            future.set_result(wire(result))
        except Exception as exc:
            result = {"state": "failed", "error": str(exc)}
            if claimed:
                # Preserve accepted child orders if an exception happened after submission.
                previous = self.store.command(key)
                if previous.get("orders"):
                    result = {**previous, "state": "unknown", "error": str(exc)}
                result = self.store.finish(key, result)
            future.set_result(result)

    def execute(self, action, p, key):
        if action == "snapshot":
            return self.snapshot()
        if action == "command":
            return self.command_status(p["id"])
        if action == "overview":
            return wire(
                [
                    {
                        "symbol": item.symbol,
                        "exchange": item.exchange,
                        "interval": item.interval,
                        "count": item.count,
                        "start": stamp(item.start),
                        "end": stamp(item.end),
                    }
                    for item in get_database().get_bar_overview()
                ]
            )
        if action == "connect":
            if self.connections_started:
                raise ValueError("连接已启动；CTP 会自动重连，修改配置后请重启交易服务")
            config = read_ctp_config()
            self.connections_started = True
            self.main.connect(config, "CTP")
            return {"message": "正在连接 SimNow"}
        if action == "subscribe":
            symbols = self.watchlist()
            contract = self.main.get_contract(p["symbol"])
            if not contract:
                raise ValueError("合约不存在或尚未加载")
            self.main.subscribe(
                SubscribeRequest(contract.symbol, contract.exchange), "CTP"
            )
            self.subscriptions.add(contract.vt_symbol)
            if contract.vt_symbol not in symbols:
                symbols.append(contract.vt_symbol)
            self.store.put("config", "watchlist", {"symbols": symbols})
            return {"symbol": contract.vt_symbol}
        if action == "watchlist_remove":
            symbols = [s for s in self.watchlist() if s != p["symbol"]]
            self.store.put("config", "watchlist", {"symbols": symbols})
            return {"symbols": symbols, "message": "已移出自选"}
        if action == "strategy_chart":
            strategy = self.cta.strategies.get(p["name"])
            doc = self.store.get("instances", p["name"])
            if not strategy or not doc:
                raise ValueError("策略不存在")
            if p["name"] in self.initializing:
                return {
                    "bars": [],
                    "indicators": {},
                    "overlays": [],
                    "trades": [],
                    "minutes": strategy.bar_minutes,
                }
            bars = list(strategy.monitor_bars)
            plots = {}
            am = strategy.am
            params = doc["parameters"]
            if "fast_window" in params and "slow_window" in params:
                plots = {
                    f"SMA{params['fast_window']}": am.sma(
                        params["fast_window"], array=True
                    ),
                    f"SMA{params['slow_window']}": am.sma(
                        params["slow_window"], array=True
                    ),
                }
            elif "boll_window" in params and "boll_dev" in params:
                upper, lower = am.boll(
                    params["boll_window"], params["boll_dev"], array=True
                )
                plots = {
                    "BOLL_UP": upper,
                    "BOLL_MID": am.sma(params["boll_window"], array=True),
                    "BOLL_LOW": lower,
                }
            return wire(
                {
                    "bars": [
                        {
                            "time": int(b.datetime.timestamp()),
                            "open": b.open_price,
                            "high": b.high_price,
                            "low": b.low_price,
                            "close": b.close_price,
                            "volume": b.volume,
                        }
                        for b in bars
                    ],
                    "minutes": strategy.bar_minutes,
                    "indicators": {
                        k: list(v[-len(bars) :])
                        if bars and am.inited
                        else [None] * len(bars)
                        for k, v in plots.items()
                    },
                    "overlays": list(plots),
                    "trades": [
                        t
                        for t in self.trade_history(doc["symbol"])
                        if t.get("strategy") == doc["name"]
                        and t.get("version") == doc["version"]
                    ],
                }
            )
        if action == "intraday":
            start = (
                datetime.fromisoformat(p["date"]).replace(tzinfo=TZ)
                if p.get("date")
                else datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            )
            end = start + timedelta(days=1)
            rows = [
                b
                for b in load_bars(p["symbol"], start, end)
                if start <= stamp(b.datetime) < end
            ]
            current = self.recorder.current.get(p["symbol"])
            if current and start <= stamp(current.datetime) < end:
                rows = [b for b in rows if b.datetime != current.datetime] + [current]
            contract = self.main.get_contract(p["symbol"])
            return {
                **intraday_points(rows, contract.size if contract else 0),
                "date": start.date().isoformat(),
                "trades": self.trade_history(p["symbol"], start, end),
            }
        if action == "bars":
            end = (
                stamp(datetime.fromisoformat(p["end"]))
                if p.get("end")
                else datetime.now(TZ)
            )
            start = (
                stamp(datetime.fromisoformat(p["start"]))
                if p.get("start")
                else end - timedelta(days=30)
            )
            if end <= start or end - start > timedelta(days=366):
                raise ValueError("行情查询区间应在 1 年以内")
            rows = load_bars(p["symbol"], start, end)
            current = self.recorder.current.get(p["symbol"])
            if current and start <= current.datetime <= end:
                rows = [b for b in rows if b.datetime != current.datetime] + [current]
            rows = aggregate(rows, p.get("minutes", 5))[-6000:]
            computed = indicators(rows, p.get("indicators"))
            rows = rows[-5000:]
            result = []
            now = datetime.now(TZ)
            for b in rows:
                result.append(
                    {
                        "time": int(b.datetime.timestamp()),
                        "open": b.open_price,
                        "high": b.high_price,
                        "low": b.low_price,
                        "close": b.close_price,
                        "volume": b.volume,
                        "open_interest": b.open_interest,
                        "complete": b.datetime + timedelta(minutes=p.get("minutes", 5))
                        <= now,
                    }
                )
            return {
                "bars": result,
                "indicators": {k: v[-5000:] for k, v in computed.items()},
                "source": "本地录制 / CSV",
                "trades": self.trade_history(p["symbol"], start, end),
            }
        if action == "import":
            rows = parse_csv(p["csv"])
            save_bars(rows)
            return {"count": len(rows), "message": "相同合约与时间的数据已覆盖"}
        if action == "publish":
            version = self.store.get("versions", p["id"])
            if not version:
                raise ValueError("策略版本不存在")
            cls = load_class(version)
            version["parameters"] = validate_parameters(cls, {})
            version["published"] = True
            self.store.put("versions", version["id"], version)
            return version
        if action == "add_instance":
            name = p["name"]
            if (
                not name
                or len(name) > 60
                or name in self.cta.strategies
                or self.store.get("instances", name)
            ):
                raise ValueError("实例名称为空、过长或已存在")
            if any(d["symbol"] == p["symbol"] for d in self.store.all("instances")):
                raise ValueError("同一合约只允许一个策略实例")
            if not self.main.get_contract(p["symbol"]):
                raise ValueError("请先连接并选择有效合约")
            version = self.store.get("versions", p["version"])
            if not version or not version["published"]:
                raise ValueError("请选择已发布的策略版本")
            cls = load_class(version)
            doc = {
                "name": name,
                "symbol": p["symbol"],
                "version": p["version"],
                "parameters": validate_parameters(cls, p.get("parameters", {})),
            }
            self.restore_instance(doc)
            self.store.put("instances", name, doc)
            self.reinit.add(name)
            return doc
        if action == "strategy":
            return self.strategy_action(p)
        if action == "reconcile":
            self.require_ready()
            if self.position_at <= self.last_trade_at:
                raise ValueError("等待持仓查询完成")
            if any(
                o.vt_symbol == p["symbol"] for o in self.main.get_all_active_orders()
            ):
                raise ValueError("仍有活动委托，请先撤单")
            if any(
                t["payload"]["symbol"] == p["symbol"] for t in self.pending.values()
            ):
                raise ValueError("平仓仍在处理")
            # No silent assignment of unexplained positions to a strategy.
            mismatch = False
            for strategy in self.cta.symbol_strategy_map.get(p["symbol"], []):
                if not self.position_matches(strategy):
                    strategy.trading = False
                    self.reinit.add(strategy.strategy_name)
                    mismatch = True
            self.blocked.discard(p["symbol"])
            for record in self.store.commands(10000):
                if record.get("symbol") == p["symbol"] and record["state"] == "unknown":
                    self.store.finish(
                        record["id"],
                        {
                            **record,
                            "state": "reconciled",
                            "message": "用户已核对柜台状态，未重发原指令",
                        },
                    )
            return {
                "message": "柜台委托已核对；策略仓位不一致，保持停止，请通过持仓列表平仓"
                if mismatch
                else "委托和持仓已核对"
            }
        if action == "cancel":
            self.require_ready()
            order = self.main.get_order(p["order_id"])
            if not order or not order.is_active():
                raise ValueError("委托不存在或已经结束")
            self.main.cancel_order(order.create_cancel_request(), "CTP")
            return {"message": "撤单已提交，请等待柜台回报"}
        if action in {"order", "close"}:
            self.require_ready()
            symbol = p["symbol"]
            if (
                p.get("price") is None
                and time.time() - self.last_tick_at.get(symbol, 0) > 30
            ):
                raise ValueError(
                    "行情超过 30 秒未更新，无法使用对手价；请填写限价或等待新行情"
                )
            if symbol in self.blocked:
                raise ValueError("该合约有未完成的平仓操作，请先核对委托")
            if any(
                d["symbol"] == symbol and d["name"] not in self.cta.strategies
                for d in self.store.all("instances")
            ):
                raise ValueError("该合约策略加载失败，请先修复策略版本")
            strategies = self.cta.symbol_strategy_map.get(symbol, [])
            if action == "order":
                if strategies:
                    raise ValueError("合约已由策略管理；手动开仓请先移除空仓策略")
                req = order_request(self.main, p, "web:" + key)
                return self.submit(key, [req])
            # Validate the intent before stopping a running strategy.
            if (
                type(p.get("volume")) is not int
                or p["volume"] <= 0
                or p.get("direction") not in {"LONG", "SHORT"}
            ):
                raise ValueError("平仓方向或手数无效")
            self.blocked.add(symbol)
            for strategy in strategies:
                self.cta.stop_strategy(strategy.strategy_name)
                strategy.trading = False
                self.reinit.add(strategy.strategy_name)
            for order in self.main.get_all_active_orders():
                if order.vt_symbol == symbol:
                    self.main.cancel_order(order.create_cancel_request(), "CTP")
            self.pending[key] = {
                "payload": p,
                "started": time.time(),
                "stage": "cancel",
                "query_after": 0,
                "orders": [],
            }
            return self.store.finish(key, {"state": "waiting_cancel", "orders": []})
        raise ValueError("未知操作")

    def command_status(self, key):
        record = self.store.command(key)
        if (
            record
            and record.get("action") == "order"
            and record["state"] == "submitted"
        ):
            orders = [self.main.get_order(oid) for oid in record.get("orders", [])]
            if orders and all(o is not None and not o.is_active() for o in orders):
                record = self.store.finish(
                    key,
                    {
                        **record,
                        "state": "completed",
                        "filled": sum(o.traded for o in orders),
                        "message": "委托已结束，实际成交数量以回报为准",
                    },
                )
        return record

    def submit(self, key, requests):
        if not requests:
            raise ValueError("没有可提交的平仓委托")
        ids = []
        self.store.finish(key, {"state": "submitting", "orders": []})
        for req in requests:
            order_id = self.main.send_order(req, "CTP")
            if not order_id:
                raise ValueError("柜台接口未接受委托，请核对订单")
            ids.append(order_id)
            self.main.update_order_request(req, order_id, "CTP")
            self.store.finish(key, {"state": "submitted", "orders": ids})
        return {"orders": ids, "message": "委托已提交，成交以柜台回报为准"}

    def restore_instance(self, doc):
        cls = load_class(self.store.get("versions", doc["version"]))
        self.cta.classes[cls.__name__] = cls
        self.cta.add_strategy(
            cls.__name__, doc["name"], doc["symbol"], doc["parameters"]
        )
        if doc["name"] not in self.cta.strategies:
            raise ValueError("创建策略实例失败")
        saved = self.store.get("strategy_state", doc["name"]) or {}
        self.cta.strategies[doc["name"]].pos = saved.get("pos", 0)

    def position_matches(self, strategy):
        positions = [
            p
            for p in self.main.get_all_positions()
            if p.vt_symbol == strategy.vt_symbol and p.volume
        ]
        long = sum(p.volume for p in positions if p.direction == Direction.LONG)
        short = sum(p.volume for p in positions if p.direction == Direction.SHORT)
        return not (long and short) and strategy.pos == long - short

    def strategy_action(self, p):
        name, action = p["name"], p["action"]
        strategy = self.cta.strategies.get(name)
        if not strategy:
            raise ValueError("策略不存在")
        if name in self.initializing:
            raise ValueError("策略正在初始化，请等待完成")
        if strategy.vt_symbol in self.blocked and action != "stop":
            raise ValueError("该合约平仓流程未结束")
        if action == "stop":
            self.cta.stop_strategy(name)
        elif action == "init":
            self.require_ready()
            if strategy.trading:
                raise ValueError("请先停止策略")
            if any(
                o.vt_symbol == strategy.vt_symbol
                for o in self.main.get_all_active_orders()
            ):
                raise ValueError("请先撤销该合约活动委托")
            if (
                not self.position_matches(strategy)
                or self.position_at <= self.last_trade_at
            ):
                raise ValueError("策略仓位与柜台不一致，请先平仓核对")
            # Fresh instance clears indicator caches; only restore explicitly tracked position.
            doc = self.store.get("instances", name)
            pos = strategy.pos
            self.cta.remove_strategy(name)
            self.restore_instance(doc)
            strategy = self.cta.strategies[name]
            strategy.pos = pos
            self.cta.errors.pop(name, None)
            self.initializing[name] = self.cta.init_strategy(name)
        elif action == "start":
            self.require_ready()
            if name in self.reinit or not strategy.inited or not strategy.ready:
                raise ValueError("请先初始化，并确保历史数据足够预热")
            if (
                not self.position_matches(strategy)
                or self.position_at < self.last_trade_at
            ):
                raise ValueError("等待持仓同步；策略持仓需与柜台一致")
            if time.time() - self.last_tick_at.get(strategy.vt_symbol, 0) > 30:
                raise ValueError("等待新行情后再启动")
            self.cta.start_strategy(name)
            if name in self.cta.errors:
                strategy.trading = False
                raise ValueError("策略启动异常，请查看日志")
        elif action == "remove":
            self.require_ready()
            if (
                not self.position_matches(strategy)
                or strategy.trading
                or strategy.pos
                or any(
                    o.vt_symbol == strategy.vt_symbol
                    for o in self.main.get_all_active_orders()
                )
            ):
                raise ValueError("移除前需停止策略、平仓并撤销活动委托")
            self.cta.remove_strategy(name)
            with self.store.db() as db:
                db.execute(
                    "DELETE FROM documents WHERE kind IN ('instances','strategy_state') AND id=?",
                    (name,),
                )
        else:
            raise ValueError("未知策略操作")
        return {"message": "操作已受理"}

    def on_tick(self, event):
        tick = event.data
        self.last_tick_at[tick.vt_symbol] = time.time()
        self.recorder.update(tick)

    def on_trade(self, event):
        self.last_trade_at = time.time()
        trade = wire(event.data)
        owner = getattr(self.cta, "orderid_strategy_map", {}).get(event.data.vt_orderid)
        if owner:
            doc = self.store.get("instances", owner.strategy_name)
            if doc:
                trade.update(strategy=doc["name"], version=doc["version"])
        self.store.put("fills", self.trade_key(trade), trade)
        for strategy in self.cta.strategies.values():
            self.store.put(
                "strategy_state", strategy.strategy_name, {"pos": strategy.pos}
            )

    @staticmethod
    def trade_key(trade):
        return f"{str(trade.get('datetime', ''))[:10]}:{trade['vt_tradeid']}:{trade['vt_symbol']}"

    def watchlist(self):
        saved = self.store.get("config", "watchlist")
        return (
            list(saved["symbols"]) if saved is not None else sorted(self.subscriptions)
        )

    def trade_history(self, symbol=None, start=None, end=None):
        records = {self.trade_key(t): t for t in self.store.all("fills")}
        for trade in self.main.get_all_trades():
            value = wire(trade)
            key = self.trade_key(value)
            records[key] = {**records.get(key, {}), **value}
        result = []
        for value in records.values():
            if symbol and value["vt_symbol"] != symbol:
                continue
            if not value.get("datetime"):
                continue
            at = stamp(datetime.fromisoformat(value["datetime"]))
            if (start and at < start) or (end and at >= end):
                continue
            result.append(value)
        return sorted(result, key=lambda t: t["datetime"])

    def on_sync(self, event):
        if event.data["kind"] == "disconnected":
            self.position_at = 0
            for strategy in self.cta.strategies.values():
                self.cta.stop_strategy(strategy.strategy_name)
                self.reinit.add(strategy.strategy_name)
            for key, task in list(self.pending.items()):
                if task["stage"] == "cancel":
                    self.store.finish(
                        key,
                        {
                            "state": "unknown",
                            "orders": [],
                            "error": "连接中断，平仓未提交；重连后请核对撤单和持仓，不会自动重发",
                        },
                    )
                    # Cancellation may still be in flight. Keep the symbol blocked
                    # until the administrator reconciles the account after reconnect.
                    del self.pending[key]
        elif event.data.get("ok"):
            self.position_at = time.time()
        else:
            self.position_at = 0

    def on_timer(self, event):
        try:
            self.recorder.expire(datetime.now(TZ))
            if self.gateway.md_api.login_status and self.gateway.td_api.contract_inited:
                for symbol in self.watchlist():
                    contract = self.main.get_contract(symbol)
                    if contract and symbol not in self.subscriptions:
                        self.main.subscribe(
                            SubscribeRequest(contract.symbol, contract.exchange), "CTP"
                        )
                        self.subscriptions.add(symbol)
            for name, future in list(self.initializing.items()):
                if not future.done():
                    continue
                strategy = self.cta.strategies.get(name)
                error = future.exception() or self.cta.errors.get(name)
                if strategy and not error and strategy.ready:
                    self.reinit.discard(name)
                elif strategy:
                    strategy.inited = False
                    self.logs.append(
                        {
                            "msg": f"{name} 初始化失败：{error or '历史数据不足，需更多已完成 K 线'}"
                        }
                    )
                del self.initializing[name]
            for key, task in list(self.pending.items()):
                self.advance_close(key, task)
        except Exception as exc:
            self.logs.append({"msg": "后台处理错误：" + str(exc)})

    def advance_close(self, key, task):
        symbol = task["payload"]["symbol"]
        try:
            if task["stage"] == "cancel" and time.time() - task["started"] > 30:
                raise ValueError("平仓准备超时，未提交委托；请核对活动委托和持仓")
            if not self.ready():
                return  # Already-submitted orders must still be reconciled.
            if task["stage"] == "cancel":
                active = [
                    o
                    for o in self.main.get_all_active_orders()
                    if o.vt_symbol == symbol
                ]
                if active:
                    if time.time() - task["started"] > 30:
                        raise ValueError("撤单尚未确认，平仓未提交；请核对活动委托")
                    return
                if not task["query_after"]:
                    task["query_after"] = time.time()
                    self.gateway.query_position()
                    return
                if self.position_at <= max(task["query_after"], self.last_trade_at):
                    if time.time() - task["query_after"] > 30:
                        raise ValueError("持仓查询未完成，平仓未提交")
                    return
                if (
                    task["payload"].get("price") is None
                    and time.time() - self.last_tick_at.get(symbol, 0) > 30
                ):
                    raise ValueError(
                        "行情超过 30 秒未更新，对手价平仓未提交；请填写限价或等待新行情"
                    )
                req = order_request(
                    self.main, task["payload"], "web-close:" + key, closing=True
                )
                reqs = self.main.convert_order_request(req, "CTP", False, False)
                task["owned_before"] = all(
                    self.position_matches(s)
                    for s in self.cta.symbol_strategy_map.get(symbol, [])
                )
                result = self.submit(key, reqs)
                task["orders"] = result["orders"]
                task["stage"] = "fills"
                return
            orders = [self.main.get_order(oid) for oid in task["orders"]]
            if any(o is None or o.is_active() for o in orders):
                return
            if task["stage"] == "fills":
                task["stage"] = "reconcile"
                task["query_after"] = time.time()
                self.gateway.query_position()
                return
            if self.position_at <= max(task["query_after"], self.last_trade_at):
                return
            positions = [
                p
                for p in self.main.get_all_positions()
                if p.vt_symbol == symbol and p.volume
            ]
            long = sum(p.volume for p in positions if p.direction == Direction.LONG)
            short = sum(p.volume for p in positions if p.direction == Direction.SHORT)
            for strategy in self.cta.symbol_strategy_map.get(symbol, []):
                if long and short:
                    raise ValueError("柜台存在双向持仓，请人工核对；策略保持停止")
                if not task.get("owned_before", False) and (long or short):
                    raise ValueError(
                        "平仓前存在无法归属的持仓；剩余持仓未分配给策略，请全部平仓后重新初始化"
                    )
                strategy.pos = long - short
                strategy.inited = False
                self.store.put(
                    "strategy_state", strategy.strategy_name, {"pos": strategy.pos}
                )
            self.store.finish(
                key,
                {
                    "state": "completed",
                    "orders": task["orders"],
                    "filled": sum(o.traded for o in orders),
                    "requested": task["payload"]["volume"],
                    "message": "委托已结束，已核对柜台持仓；策略需重新初始化",
                    "submission_error": task.get("submission_error"),
                },
            )
            self.blocked.discard(symbol)
            del self.pending[key]
        except Exception as exc:
            previous = self.store.command(key)
            self.store.finish(
                key,
                {
                    **previous,
                    "state": "failed" if not previous.get("orders") else "unknown",
                    "error": str(exc),
                },
            )
            if previous.get("orders") and task["stage"] == "cancel":
                # A split order can be only partly submitted. Track accepted orders; never resend the rest.
                task.update(
                    stage="fills", orders=previous["orders"], submission_error=str(exc)
                )
                return
            if not previous.get("orders") or task["stage"] == "reconcile":
                self.blocked.discard(symbol)
            del self.pending[key]

    def snapshot(self):
        instances = []
        for doc in self.store.all("instances"):
            strategy = self.cta.strategies.get(doc["name"])
            instances.append(
                {
                    **doc,
                    "inited": bool(strategy and strategy.inited),
                    "trading": bool(strategy and strategy.trading),
                    "ready": bool(strategy and strategy.ready),
                    "needs_init": doc["name"] in self.reinit,
                    "initializing": doc["name"] in self.initializing,
                    "pos": strategy.pos if strategy else 0,
                    "variables": wire(strategy.get_variables()) if strategy else {},
                    "error": self.cta.errors.get(doc["name"]),
                }
            )
        return wire(
            {
                "environment": "SimNow",
                "ready": self.ready(),
                "connected": self.connections_started,
                "md": self.gateway.md_api.login_status,
                "td": self.gateway.td_api.login_status,
                "position_synced": self.position_at,
                "accounts": self.main.get_all_accounts(),
                "positions": self.main.get_all_positions(),
                "orders": self.main.get_all_orders(),
                "trades": self.trade_history(),
                "ticks": self.main.get_all_ticks(),
                "tick_received_at": self.last_tick_at,
                "contracts": self.main.get_all_contracts(),
                "strategies": instances,
                "operations": self.store.commands(),
                "blocked_symbols": sorted(self.blocked),
                "logs": self.logs[-200:],
                "subscriptions": sorted(self.subscriptions),
                "watchlist": self.watchlist(),
            }
        )

    def publish(self, event):
        if event.type in {EVENT_LOG, "eCtaLog"}:
            self.logs.append(wire(event.data))
            self.logs = self.logs[-200:]
        if (
            event.type.startswith(
                ("eTick.", "eOrder.", "eTrade.", "ePosition.", "eAccount.")
            )
            and event.type.count(".") == 1
            or event.type in {"eCtaStrategy", "eCtaLog", EVENT_LOG}
        ):
            if self.rpc.is_active():
                message = {"type": event.type, "data": wire(event.data)}
                if event.type == EVENT_TICK:
                    message["received_at"] = self.last_tick_at.get(event.data.vt_symbol)
                self.rpc.publish("event", message)


def main():
    import fcntl

    from .config import RUNTIME

    lock = open(RUNTIME / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("这个运行目录已有交易进程，请勿重复启动") from exc
    stopped = ThreadEvent()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    worker = Worker()
    worker.start()
    print("交易进程已启动；等待网页连接 SimNow", flush=True)
    stopped.wait()
    worker.close()


if __name__ == "__main__":
    main()
