"""One backtest per subprocess, pinned code and an immutable bar-data snapshot."""

import gzip
import hashlib
import json
import sys
import traceback
from datetime import datetime

from .config import RUNTIME, prepare_runtime

prepare_runtime()

from vnpy.trader.constant import Interval
from vnpy_ctastrategy.backtesting import BacktestingEngine

from .market import aggregate, load_bars, stamp
from .store import Store, wire
from .strategies import load_class, validate_parameters


def run(job_id):
    store = Store()
    job = store.get("backtests", job_id)
    logs = []
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (300, 310))
        job.update(state="running")
        store.put("backtests", job_id, job)
        p = job["parameters"]
        version = store.get("versions", p["version"])
        cls = load_class(version)
        settings = validate_parameters(cls, p["settings"])
        start, end = (
            stamp(datetime.fromisoformat(p["start"])),
            stamp(datetime.fromisoformat(p["end"])),
        )
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        bars = load_bars(p["symbol"], start, end)
        if not bars:
            raise ValueError("该时间区间没有一分钟历史数据，请先导入 CSV 或录制行情")
        # A fixed in-memory history prevents concurrent recording/import changing a running test.
        engine = BacktestingEngine()
        engine.output = logs.append
        engine.set_parameters(
            vt_symbol=p["symbol"],
            interval=Interval.MINUTE,
            start=start,
            end=end,
            rate=p["rate"],
            slippage=p["slippage"],
            size=p["size"],
            pricetick=p["pricetick"],
            capital=p["capital"],
        )
        engine.add_strategy(cls, settings)
        engine.history_data = bars
        # Preload warming history too, and make missing warmup an explicit failure.
        from datetime import timedelta

        warmup = load_bars(
            p["symbol"], start - timedelta(days=30), start - timedelta(microseconds=1)
        )
        snapshot = json.dumps(
            wire({"warmup": warmup, "bars": bars}), sort_keys=True
        ).encode()
        with gzip.open(RUNTIME / (job_id + ".bars.json.gz"), "wb") as output:
            output.write(snapshot)
        engine.load_bar = lambda *args, **kwargs: warmup
        strategy = engine.strategy
        strategy.on_init()
        if not strategy.ready:
            raise ValueError(
                "回测开始日期之前的 30 天数据不足以预热策略，请补充历史数据或后移开始日期"
            )
        strategy.inited = True
        strategy.on_start()
        strategy.trading = True
        for index, bar in enumerate(bars):
            engine.new_bar(bar)
            if index % 10000 == 0:
                job["progress"] = round(index / len(bars), 3)
                store.put("backtests", job_id, job)
        strategy.on_stop()
        frame = engine.calculate_result()
        stats = engine.calculate_statistics(frame, output=False)
        equity = []
        if frame is not None:
            for day, row in frame.iterrows():
                equity.append(
                    {
                        "date": str(day),
                        "balance": row.get("balance"),
                        "net_pnl": row.get("net_pnl"),
                        "drawdown": row.get("drawdown"),
                    }
                )
        data_hash = hashlib.sha256(snapshot).hexdigest()
        chart_bars = aggregate(bars, settings["bar_minutes"])[-5000:]
        job.update(
            state="completed",
            progress=1,
            statistics=wire(stats),
            equity=wire(equity),
            trades=wire(engine.get_all_trades()),
            logs=logs[-100:],
            data_hash=data_hash,
            code_hash=version["sha256"],
            bar_count=len(bars),
            warmup_count=len(warmup),
            chart={
                "bars": [
                    {
                        "time": int(bar.datetime.timestamp()),
                        "open": bar.open_price,
                        "high": bar.high_price,
                        "low": bar.low_price,
                        "close": bar.close_price,
                        "volume": bar.volume,
                    }
                    for bar in chart_bars
                ],
                "minutes": settings["bar_minutes"],
            },
        )
    except Exception as exc:
        job.update(
            state="failed", error=str(exc), logs=logs[-100:] + [traceback.format_exc()]
        )
    store.put("backtests", job_id, job)


if __name__ == "__main__":
    run(sys.argv[1])
