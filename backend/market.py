import csv
import io
import math
from copy import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import talib
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData

from .store import wire

TZ = ZoneInfo("Asia/Shanghai")
PERIODS = {1, 5, 15, 30, 60}


def stamp(dt):
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def bucket(dt, minutes):
    local = stamp(dt)
    return local.replace(
        minute=local.minute // minutes * minutes, second=0, microsecond=0
    )


def aggregate(bars, minutes):
    if minutes not in PERIODS:
        raise ValueError("不支持的 K 线周期")
    result = []
    for bar in sorted(bars, key=lambda x: x.datetime):
        start = bucket(bar.datetime, minutes)
        if not result or result[-1].datetime != start:
            merged = copy(bar)
            merged.datetime = start
            result.append(merged)
        else:
            merged = result[-1]
            merged.high_price = max(merged.high_price, bar.high_price)
            merged.low_price = min(merged.low_price, bar.low_price)
            merged.close_price = bar.close_price
            merged.volume += bar.volume
            merged.turnover += bar.turnover
            merged.open_interest = bar.open_interest
    return result


class MinuteRecorder:
    """No synthetic bars; ignore out-of-order ticks and reset cumulative counters at gaps."""

    def __init__(self, callback):
        self.callback = callback
        self.current = {}
        self.last = {}
        self.closed = {}

    def update(self, tick):
        dt = stamp(tick.datetime)
        key = tick.vt_symbol
        start = bucket(dt, 1)
        prev = self.last.get(key)
        if not math.isfinite(tick.last_price) or tick.last_price <= 0:
            return
        if prev and dt < stamp(prev.datetime):
            return
        if key in self.closed and start <= self.closed[key]:
            return
        bar = self.current.get(key)
        if bar and bar.datetime != start:
            self.flush(key)
            bar = None
        if not bar:
            bar = BarData(
                symbol=tick.symbol,
                exchange=tick.exchange,
                datetime=start,
                gateway_name="CTP",
                interval=Interval.MINUTE,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
            )
            self.current[key] = bar
        bar.high_price = max(bar.high_price, tick.last_price)
        bar.low_price = min(bar.low_price, tick.last_price)
        bar.close_price = tick.last_price
        bar.open_interest = tick.open_interest
        if prev and (dt - stamp(prev.datetime)).total_seconds() < 120:
            bar.volume += max(0, tick.volume - prev.volume)
            bar.turnover += max(0, tick.turnover - prev.turnover)
        self.last[key] = copy(tick)

    def flush(self, key):
        bar = self.current.pop(key, None)
        if bar:
            self.closed[key] = bar.datetime
            self.callback(bar)

    def expire(self, now):
        for key, bar in list(self.current.items()):
            if stamp(now) >= bar.datetime + timedelta(minutes=1, seconds=2):
                self.flush(key)


def load_bars(symbol, start, end):
    code, exchange = symbol.split(".")
    return get_database().load_bar_data(
        code, Exchange[exchange], Interval.MINUTE, stamp(start), stamp(end)
    )


def save_bars(bars):
    # vnpy_sqlite mutates its input and updates one contract's overview per call.
    from collections import defaultdict

    grouped = defaultdict(list)
    for bar in bars:
        grouped[bar.vt_symbol].append(copy(bar))
    database = get_database()
    with database.db.atomic():
        for group in grouped.values():
            database.save_bar_data(group)


def intraday_points(bars, size=0):
    """VWAP for the recorded interval; CSV without turnover uses close * volume."""
    volume = 0
    value = 0
    estimated = False
    points = []
    for bar in sorted(bars, key=lambda b: b.datetime):
        amount = max(0, bar.volume)
        if amount:
            exact = size > 0 and bar.turnover > 0
            value += bar.turnover / size if exact else bar.close_price * amount
            volume += amount
            estimated = estimated or not exact
        points.append(
            {
                "time": int(stamp(bar.datetime).timestamp()),
                "close": bar.close_price,
                "open": bar.open_price,
                "volume": amount,
                "average": value / volume if volume else None,
            }
        )
    return {"bars": points, "estimated": estimated}


def indicators(bars, params=None):
    p = {
        "ma": 20,
        "ema": 20,
        "boll": 20,
        "dev": 2.0,
        "rsi": 14,
        "atr": 14,
        "fast": 12,
        "slow": 26,
        "signal": 9,
        **(params or {}),
    }
    for key in ("ma", "ema", "boll", "rsi", "atr", "fast", "slow", "signal"):
        p[key] = int(p[key])
        if not 2 <= p[key] <= 500:
            raise ValueError("指标周期必须在 2～500 之间")
    if p["fast"] >= p["slow"] or not 0 < float(p["dev"]) <= 10:
        raise ValueError("MACD 快线必须小于慢线；布林标准差需在 0～10 之间")
    close = np.array([b.close_price for b in bars], dtype=float)
    high = np.array([b.high_price for b in bars], dtype=float)
    low = np.array([b.low_price for b in bars], dtype=float)
    if not len(bars):
        return {}
    upper, mid, lower = talib.BBANDS(close, p["boll"], float(p["dev"]), float(p["dev"]))
    dif, dea, hist = talib.MACD(close, p["fast"], p["slow"], p["signal"])
    values = {
        "MA": talib.SMA(close, p["ma"]),
        "EMA": talib.EMA(close, p["ema"]),
        "BOLL_UP": upper,
        "BOLL_MID": mid,
        "BOLL_LOW": lower,
        "MACD": dif,
        "SIGNAL": dea,
        "HIST": hist,
        "RSI": talib.RSI(close, p["rsi"]),
        "ATR": talib.ATR(high, low, close, p["atr"]),
    }
    return {key: wire(list(value)) for key, value in values.items()}


def parse_csv(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {
        "symbol",
        "exchange",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("CSV 列必须包含：" + ", ".join(sorted(required)))
    bars = {}
    for line, row in enumerate(reader, 2):
        if line > 200002:
            raise ValueError("单次导入不能超过 20 万行")
        try:
            dt = stamp(datetime.fromisoformat(row["datetime"]))
            if dt.second or dt.microsecond:
                raise ValueError("时间应为一分钟 K 线的起始时间")
            values = {
                k: float(row[k]) for k in ("open", "high", "low", "close", "volume")
            }
            if not all(math.isfinite(v) and v >= 0 for v in values.values()):
                raise ValueError("存在负数或非有限数值")
            if min(values[k] for k in ("open", "high", "low", "close")) <= 0:
                raise ValueError("价格必须大于 0")
            if (
                not values["low"]
                <= min(values["open"], values["close"])
                <= max(values["open"], values["close"])
                <= values["high"]
            ):
                raise ValueError("OHLC 高低价不一致")
            oi = float(row.get("open_interest") or 0)
            if not math.isfinite(oi) or oi < 0:
                raise ValueError("持仓量无效")
            if not row["symbol"].isalnum():
                raise ValueError("合约代码无效")
            b = BarData(
                symbol=row["symbol"],
                exchange=Exchange[row["exchange"]],
                datetime=dt,
                gateway_name="CSV",
                interval=Interval.MINUTE,
                open_price=values["open"],
                high_price=values["high"],
                low_price=values["low"],
                close_price=values["close"],
                volume=values["volume"],
                open_interest=oi,
            )
            bars[(b.vt_symbol, dt)] = b
        except (ValueError, KeyError) as exc:
            raise ValueError(f"CSV 第 {line} 行：{exc}") from exc
    if not bars:
        raise ValueError("CSV 没有数据行")
    return sorted(bars.values(), key=lambda b: (b.vt_symbol, b.datetime))
