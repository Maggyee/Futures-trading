"""Import recent real-contract minute bars from Sina's public quote service."""

import argparse
import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

from .api import rpc_call
from .config import RUNTIME
from .market import TZ, parse_csv

DEFAULT_SYMBOLS = [
    "rb2701.SHFE",
    "m2701.DCE",
    "cu2611.SHFE",
    "au2612.SHFE",
    "sc2612.INE",
]
ENDPOINT = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine"
FIELDS = [
    "symbol",
    "exchange",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
]


def normalize(raw, symbol, now=None):
    code, exchange = symbol.split(".")
    # Reject continuous-contract aliases: never map RB0 history onto a delivery contract.
    if not re.fullmatch(r"[A-Za-z]+(?:\d{3}|\d{4})", code):
        raise ValueError("只支持具体交割合约，不接受主连代码")
    match = re.search(r"=\s*\((.*)\)\s*;?\s*$", raw, re.S)
    if not match:
        raise ValueError("历史服务响应格式不正确")
    rows = json.loads(match.group(1))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{symbol} 未返回历史数据")
    now = now or datetime.now(TZ)
    result = []
    for row in rows:
        # The provider labels the closing minute (09:01, ..., 15:00).
        # vn.py and our CSV schema label the opening minute (09:00, ..., 14:59).
        end = datetime.fromisoformat(row["d"]).replace(tzinfo=TZ)
        if end.second or end.microsecond:
            raise ValueError("历史服务返回非整分钟时间戳")
        if end > now - timedelta(minutes=1):
            continue  # Exclude unfinished/just-finalizing public bars.
        result.append(
            {
                "symbol": code,
                "exchange": exchange,
                "datetime": (end - timedelta(minutes=1)).isoformat(),
                **{
                    name: row[key]
                    for name, key in [
                        ("open", "o"),
                        ("high", "h"),
                        ("low", "l"),
                        ("close", "c"),
                        ("volume", "v"),
                    ]
                },
                "open_interest": row.get("p", 0),
            }
        )
    if not result:
        raise ValueError("没有已完成的历史 K 线")
    # Reuse strict OHLC, volume, timestamp and symbol validation before any write.
    parse_csv(to_csv(result))
    return sorted(result, key=lambda row: row["datetime"])


def to_csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def import_symbol(symbol, folder):
    code = symbol.split(".")[0]
    url = ENDPOINT + "?" + urllib.parse.urlencode({"symbol": code.upper(), "type": "1"})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(4_000_000).decode()
    rows = normalize(raw, symbol)
    (folder / f"{symbol}.raw.txt").write_text(raw)
    (folder / f"{symbol}.csv").write_text(to_csv(rows))
    existing = rpc_call(
        "bars",
        {
            "symbol": symbol,
            "minutes": 1,
            "start": rows[0]["datetime"],
            "end": (
                datetime.fromisoformat(rows[-1]["datetime"]) + timedelta(minutes=1)
            ).isoformat(),
        },
    )
    if existing.get("state") == "failed":
        raise ValueError(existing.get("error"))
    occupied = {bar["time"] for bar in existing["bars"]}
    missing = [
        row
        for row in rows
        if int(datetime.fromisoformat(row["datetime"]).timestamp()) not in occupied
    ]
    report = {
        "symbol": symbol,
        "source": "新浪期货公开一分钟行情",
        "url": url,
        "fetched_at": datetime.now(TZ).isoformat(),
        "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source_count": len(rows),
        "start": rows[0]["datetime"],
        "end": rows[-1]["datetime"],
        "existing_skipped": len(rows) - len(missing),
        "imported": 0,
        "timestamp_conversion": "Sina minute end minus one minute -> bar start, Asia/Shanghai",
        "average": "估算：源数据不含成交额",
        "state": "prepared",
    }
    path = folder / f"{symbol}.manifest.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if missing:
        key = str(uuid.uuid4())
        report["command_id"] = key
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        result = rpc_call("import", {"csv": to_csv(missing)}, key)
        if result.get("state") != "completed":
            raise ValueError(str(result))
        report["imported"] = result["result"]["count"]
    report["state"] = "completed"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(
        description="导入新浪公开的近期一分钟历史数据，保留已有本地记录"
    )
    parser.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS)
    args = parser.parse_args()
    snapshot = rpc_call("snapshot")
    contracts = {c["vt_symbol"] for c in snapshot["contracts"]}
    if any(symbol not in contracts for symbol in args.symbols):
        raise SystemExit("请先连接 SimNow 并确认所选交割合约存在")
    if any(s["trading"] for s in snapshot["strategies"]):
        raise SystemExit("请先停止策略，再导入历史数据")
    folder = RUNTIME / "history" / datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    failed = False
    for symbol in args.symbols:
        try:
            report = import_symbol(symbol, folder)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        except Exception as exc:
            failed = True
            print(
                json.dumps(
                    {"symbol": symbol, "state": "failed", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(f"原始响应、CSV 与来源记录：{folder}", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
