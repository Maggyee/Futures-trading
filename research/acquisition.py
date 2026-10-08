"""Anonymous EDB monthly acquisition. Explicit scope, serial requests, verifiable resume."""

import csv
import json
import math
import os
import re
import selectors
import subprocess
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .calendar import TZ, Calendar
from .config import ResearchError, digest
from .data import DailyObservation, Dataset, file_sha256
from .storage import SpaceBudget

DOCUMENTATION = "https://doc.shinnytech.com/edb/latest/md_server.html"
CATALOG_URL = "https://openmd.shinnytech.com/t/md/symbols/latest.json"
HOLIDAYS_URL = "https://files.shinnytech.com/shinny_chinese_holiday.json"
EDB_URL = "https://edb.shinnytech.com/md/kline"
EXCHANGES = {"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}


def atomic_json(path, value, budget=None):
    path = Path(path)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    temp = path.with_suffix(path.suffix + ".partial")
    if budget:
        budget.check(temp, reserve=len(body))
    temp.write_bytes(body)
    temp.replace(path)


class DownloadAllowance:
    """Shared daily request/byte ceilings across the declared expansion months."""
    def __init__(self, policy):
        self.policy = policy
        self.path = Path(policy["ledger"])
        if any(type(policy[k]) is not int or policy[k] < 1 for k in ("max_bytes_per_day", "max_requests_per_day")):
            raise ResearchError("下载日限额必须为正整数")

    def consume(self, count=0, requests=0):
        ledger = json.loads(self.path.read_text()) if self.path.exists() else {}
        day = datetime.now(TZ).date().isoformat()
        used = ledger.get(day, {"bytes": 0, "requests": 0})
        if used["bytes"] + count > self.policy["max_bytes_per_day"] or used["requests"] + requests > self.policy["max_requests_per_day"]:
            raise ResearchError("达到公开下载每日预算，保留进度等待次日")
        ledger[day] = {"bytes": used["bytes"] + count, "requests": used["requests"] + requests}
        atomic_json(self.path, ledger)


class PublicDownloader:
    def __init__(self, root, budget, timeout=40, max_response_bytes=32 * 1024 * 1024, download_allowance=None):
        self.root, self.budget = Path(root).resolve(), budget
        self.timeout, self.cap = timeout, max_response_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.request_count = 0
        self.http = None
        self.allowance = DownloadAllowance(download_allowance) if download_allowance else None

    def charge_download(self, count=0, requests=0):
        if self.allowance:
            self.allowance.consume(count, requests)

    def _fetch_edb(self, url, path, receipt, cap, timeout):
        import httpx

        if self.http is None:
            # HTTPX does not read .netrc. A persistent anonymous connection avoids repeated handshakes.
            self.http = httpx.Client(
                timeout=httpx.Timeout(10, connect=8), follow_redirects=True
            )
        partial = path.with_suffix(path.suffix + ".partial")
        started, size, error = time.monotonic(), 0, None
        self.charge_download(requests=1)
        self.request_count += 1
        try:
            with self.http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ResearchError(f"公开行情 HTTP 状态：{response.status_code}")
                with partial.open("wb") as stream:
                    for block in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() - started > timeout:
                            raise ResearchError("公开行情响应超时")
                        size += len(block)
                        self.charge_download(len(block))
                        if size > cap:
                            raise ResearchError("单次响应超过大小上限")
                        self.budget.check(partial, reserve=len(block))
                        stream.write(block)
                        stream.flush()
                partial.replace(path)
                saved = {
                    "url": url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": size,
                    "sha256": file_sha256(path),
                    "requested_utc": datetime.now(timezone.utc).isoformat(),
                    "seconds": round(time.monotonic() - started, 3),
                    "credential_used": False,
                }
                atomic_json(receipt, saved, self.budget)
            return path, False
        except httpx.HTTPError as exc:
            error = f"公开行情网络错误：{type(exc).__name__}"
            raise ResearchError(error) from exc
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            with (self.root / "download_attempts.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "url": url,
                            "path": str(path),
                            "bytes": size,
                            "error": error,
                            "seconds": round(time.monotonic() - started, 3),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def fetch(self, url, relative, cap=None, timeout=None):
        # This adapter never reads environment tokens, gateway settings, or trading accounts.
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "edb.shinnytech.com",
                "openmd.shinnytech.com",
                "files.shinnytech.com",
            }
            or parsed.username
            or parsed.password
            or "token" in parsed.query.lower()
        ):
            raise ResearchError("公开下载器仅允许已核对的匿名官方 HTTPS 接口")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ResearchError("下载路径超出本轮目录")
        receipt = path.with_suffix(path.suffix + ".receipt.json")
        if path.exists() and receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved["url"] != url or file_sha256(path) != saved["sha256"]:
                raise ResearchError(
                    f"已保存的公开行情校验失败：{path.name}，不自动覆盖"
                )
            return path, True
        if path.exists():
            raise ResearchError(
                f"已有文件没有下载校验凭据：{path}，请保留并换用新目标路径"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        headers = path.with_suffix(path.suffix + ".headers.partial")
        self.budget.check(partial, reserve=65536)
        cap = cap or self.cap
        if parsed.hostname == "edb.shinnytech.com":
            try:
                return self._fetch_edb(url, path, receipt, cap, timeout or self.timeout)
            except ImportError:
                pass  # curl fallback for environments without the optional HTTPX package.
        started = time.monotonic()
        self.charge_download(requests=1)
        command = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--connect-timeout",
            "8",
            "--max-time",
            str(timeout or self.timeout),
            "--dump-header",
            str(headers),
            "--output",
            "-",
            url,
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        size = 0
        error = None
        self.request_count += 1
        try:
            with partial.open("wb") as stream:
                while True:
                    ready = selector.select(timeout=1)
                    self.budget.check(partial, reserve=65536)
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        break
                    size += len(block)
                    self.charge_download(len(block))
                    if size > cap:
                        raise ResearchError("单次响应超过大小上限，停止下载")
                    stream.write(block)
                    stream.flush()
            process.wait(timeout=10)
            stderr = process.stderr.read(2048).decode(errors="replace").strip()
            if process.returncode:
                raise ResearchError(
                    f"公开接口下载失败（curl {process.returncode}）：{stderr}"
                )
            status = None
            content_type = None
            if headers.exists():
                for line in headers.read_text(errors="replace").splitlines():
                    if line.startswith("HTTP/"):
                        status = int(line.split()[1])
                    elif line.lower().startswith("content-type:"):
                        content_type = line.split(":", 1)[1].strip()
            if status != 200:
                raise ResearchError(f"公开接口 HTTP 状态无效：{status}")
            partial.replace(path)
            saved = {
                "url": url,
                "status": status,
                "content_type": content_type,
                "bytes": size,
                "sha256": file_sha256(path),
                "requested_utc": datetime.now(timezone.utc).isoformat(),
                "seconds": round(time.monotonic() - started, 3),
                "credential_used": False,
            }
            atomic_json(receipt, saved, self.budget)
            return path, False
        except Exception as exc:
            error = str(exc)
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            with (self.root / "download_attempts.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(
                    json.dumps(
                        {
                            "url": url,
                            "path": str(path),
                            "bytes": size,
                            "error": error,
                            "seconds": round(time.monotonic() - started, 3),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            # Incomplete .partial is counted in the budget and never treated as completed data.
            if headers.exists():
                headers.unlink()


def iter_catalog(path):
    """Stream a top-level JSON dictionary; reject truncated downloads and trailing garbage."""
    decoder = json.JSONDecoder()
    with Path(path).open(encoding="utf-8") as stream:
        buffer, pos, eof = "", 0, False

        def refill():
            nonlocal buffer, pos, eof
            buffer = buffer[pos:] + stream.read(65536)
            pos = 0
            eof = stream.tell() == os.fstat(stream.fileno()).st_size
            if len(buffer) > 4 * 1024 * 1024:
                raise ResearchError("合约目录单项过大或格式不正确")

        def skip():
            nonlocal pos
            while True:
                while pos < len(buffer) and buffer[pos].isspace():
                    pos += 1
                if pos < len(buffer) or eof:
                    return
                refill()

        def char(wanted):
            nonlocal pos
            skip()
            if pos >= len(buffer) or buffer[pos] != wanted:
                raise ResearchError("合约目录格式错误或下载不完整")
            pos += 1

        def value():
            nonlocal pos
            skip()
            while True:
                try:
                    decoded, end = decoder.raw_decode(buffer, pos)
                    pos = end
                    return decoded
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ResearchError("合约目录下载不完整") from exc
                    refill()

        refill()
        char("{")
        first = True
        while True:
            skip()
            if pos < len(buffer) and buffer[pos] == "}":
                pos += 1
                break
            if not first:
                char(",")
            key = value()
            char(":")
            record = value()
            if not isinstance(key, str) or not isinstance(record, dict):
                raise ResearchError("合约目录应为代码到元数据的映射")
            yield key, record
            first = False
        skip()
        if pos != len(buffer):
            raise ResearchError("合约目录尾部有非 JSON 内容")


def month_bounds(month):
    start = date.fromisoformat(month + "-01")
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    if end > datetime.now(TZ).date():
        raise ResearchError("本命令仅接入已结束月份，禁止把未收市日线当作完整数据")
    return start, end


def monthly_calendar(month, holidays, warmup_days):
    start, end = month_bounds(month)
    if not 0 <= warmup_days <= 5:
        raise ResearchError("月前分钟预热上限为 5 个交易日")
    years = {date.fromisoformat(d).year for d in holidays}
    if start.year not in years:
        raise ResearchError("公开节假日日历不覆盖研究年份，禁止猜测交易日")
    closed = set(holidays)
    available = []
    cursor = start - timedelta(days=30)
    while cursor < end:
        if cursor.weekday() < 5 and cursor.isoformat() not in closed:
            available.append(cursor.isoformat())
        cursor += timedelta(days=1)
    past = [d for d in available if d < start.isoformat()]
    # One extra daily-only predecessor; no minute requests on this date.
    days = past[-(warmup_days + 1) :] + [d for d in available if d >= start.isoformat()]
    return {
        "trading_days": days,
        "profiles": {},
        "overrides": {},
        "night_dates": {},
        "verified": False,
        "source": HOLIDAYS_URL,
        "note": "供应商历史节假日与最新合约时段；尚未逐交易所核实历史特殊安排",
    }


def catalogue_contracts(path, calendar, month, product_scope=None):
    start, _ = month_bounds(month)
    earliest = calendar["trading_days"][0]
    result, excluded, indexes = [], [], set()
    for provider_symbol, item in iter_catalog(path):
        if item.get("class") == "FUTURE_INDEX" and provider_symbol.startswith("KQ.i@"):
            indexes.add(provider_symbol[5:])
        if item.get("class") != "FUTURE":
            continue
        match = re.fullmatch(r"([A-Z]+)\.([A-Za-z]+)([0-9]{3,4})", provider_symbol)
        if not match or match[1] not in EXCHANGES:
            continue
        exchange, product = match[1], match[2]
        if product_scope and f"{exchange}.{product}" not in product_scope:
            continue
        expiration = item.get("expire_datetime")
        if not isinstance(expiration, (float, int)) or not math.isfinite(expiration):
            excluded.append({"contract": provider_symbol, "reason": "expiry_missing"})
            continue
        expiry = datetime.fromtimestamp(expiration, TZ).date().isoformat()
        if expiry < earliest:
            continue
        schedule = item.get("trading_time", {}).get("day")
        if not schedule:
            excluded.append(
                {"contract": provider_symbol, "reason": "day_sessions_missing"}
            )
            continue
        schedule = [[a[:5], b[:5]] for a, b in schedule]
        profile = "schedule_" + digest(schedule)[:10]
        calendar["profiles"][profile] = schedule
        group = "financial" if exchange == "CFFEX" else "commodity"
        result.append(
            {
                "symbol": match[2] + match[3],
                "exchange": exchange,
                "provider_symbol": provider_symbol,
                "product": product,
                "group": group,
                "session_profile": profile,
                "time_profile": "treasury"
                if exchange == "CFFEX" and product in {"T", "TF", "TS", "TL"}
                else "index"
                if exchange == "CFFEX"
                else "commodity",
                "effective_from": earliest,
                "listed": None,
                "expiry": expiry,
                "tick_size": item.get("price_tick"),
                "value_per_price": item.get("volume_multiple"),
                "turnover_factor": None,
                "margin_rate": None,
                "fees": [],
                "verified": False,
                "listed_basis": "尚未获取历史上市日期；下载日线后仅使用窗口内首个已观察日代理",
                "source": CATALOG_URL,
                "source_asof": datetime.fromtimestamp(
                    Path(path).stat().st_mtime, TZ
                ).isoformat(),
                "expiry_before_research_month": expiry < start.isoformat(),
            }
        )
    return sorted(result, key=lambda x: x["provider_symbol"]), excluded, sorted(indexes)


def edb_url(symbol, period, start, end):
    if not re.fullmatch(
        r"(?:SHFE|DCE|CZCE|CFFEX|INE|GFEX)\.[A-Za-z]+[0-9]{3,4}", symbol
    ):
        raise ResearchError("公开行情请求必须指定真实期货交割合约")
    if period not in {60, 86400}:
        raise ResearchError("本轮仅允许分钟/日线，不允许 Tick")
    return (
        EDB_URL
        + "?"
        + urlencode(
            {"period": period, "symbol": symbol, "start_time": start, "end_time": end}
        )
    )


def edb_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        needed = {
            "datetime_nano",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_oi",
            "close_oi",
        }
        if not needed <= set(reader.fieldnames or []):
            raise ResearchError("EDB 响应缺少官方分钟/日线字段")
        for row in reader:
            nano = int(row["datetime_nano"])
            if nano % 1000000000:
                raise ResearchError("EDB 时间戳不是整秒")
            dt = datetime.fromtimestamp(nano // 1000000000, TZ)
            values = {k: float(row[k]) for k in needed - {"datetime_nano"}}
            if any(not math.isfinite(v) or v < 0 for v in values.values()):
                raise ResearchError("EDB 行情存在非有限值或负值")
            if (
                not 0
                < values["low"]
                <= min(values["open"], values["close"])
                <= max(values["open"], values["close"])
                <= values["high"]
            ):
                raise ResearchError("EDB 行情 OHLC 异常")
            yield dt, values


def normalize_daily(path, contract, calendar):
    rows = []
    seen = set()
    for dt, values in edb_rows(path):
        if dt.time().isoformat() != "00:00:00":
            raise ResearchError("公开日线不是交易日零点标记，需重新核查供应商口径")
        day = dt.date().isoformat()
        if day not in calendar["trading_days"]:
            continue
        if day in seen:
            raise ResearchError("公开日线出现重复交易日")
        seen.add(day)
        rows.append(
            {
                "trading_day": day,
                "exchange": contract["exchange"],
                "symbol": contract["symbol"],
                "product": contract["product"],
                "volume": values["volume"],
                "open_interest": values["close_oi"],
                "complete": True,
                "provenance": "REAL_EDB_PUBLIC",
            }
        )
    return rows


def minute_plan(pool, calendar, warmup_days):
    """Warm each newly active real contract using its own previous trading days."""
    days = calendar["trading_days"]
    requested = {}
    previous = {}
    for row in sorted(pool, key=lambda r: (r["date"], r["group"], r["product"])):
        key = row["contract"]
        identity = row["group"], row["product"]
        index = days.index(row["date"])
        if previous.get(identity) != key:
            for day in days[max(1, index - warmup_days) : index]:
                requested.setdefault((day, key), "warmup")
        requested[(row["date"], key)] = "research"
        previous[identity] = key
    return [
        {"trading_day": day, "contract": key, "purpose": purpose}
        for (day, key), purpose in sorted(requested.items())
    ]


def normalize_minutes(path, contract, calendar, expected):
    rows, removed = [], Counter()
    cal = Calendar(calendar)
    days = {r["trading_day"] for r in expected}
    seen = set()
    for dt, values in edb_rows(path):
        day = dt.date().isoformat()
        if day not in days or not cal.locate(dt, day, contract):
            removed["outside_requested_day_sessions"] += 1
            continue
        if dt.second or dt.microsecond or dt in seen:
            raise ResearchError("公开分钟重复或时间戳非整分钟")
        seen.add(dt)
        rows.append(
            {
                "datetime": dt.isoformat(),
                "trading_day": day,
                "exchange": contract["exchange"],
                "symbol": contract["symbol"],
                "product": contract["product"],
                **{k: values[k] for k in ("open", "high", "low", "close", "volume")},
                "open_interest": values["close_oi"],
                "turnover": None,
                "session_open_oi": values["open_oi"]
                if dt == cal.bounds(day, contract)[0]
                else None,
                "tradable": True,
                "provenance": "REAL_EDB_PUBLIC",
            }
        )
    return rows, dict(removed)


def write_parquet(path, rows, budget):
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not rows:
        raise ResearchError("不把空行情写成已完成分区")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".partial")
    # Conservative upper bound also covers temporary encoding work; never duplicate final partitions.
    budget.check(temp, reserve=max(65536, len(rows) * 1024))
    pq.write_table(pa.Table.from_pylist(rows), temp, compression="zstd")
    budget.check(temp)
    temp.replace(path)


def acquisition_config(path):
    path = Path(path).resolve()
    settings = json.loads(path.read_text())
    month_bounds(settings["month"])
    settings["root"] = str((path.parent / settings.get("root", ".")).resolve())
    budget = settings["budget"]
    budget["roots"] = [str((path.parent / p).resolve()) for p in budget["roots"]]
    if budget["max_bytes"] > 5 * 1024**3 or budget["min_free_bytes"] < 2 * 1024**3:
        raise ResearchError("本轮最多新增 5GiB，必须至少保留 2GiB")
    if settings.get("warmup_days", 5) > 5:
        raise ResearchError("本轮预热最多 5 个交易日")
    if settings.get("workers", 1) != 1:
        raise ResearchError("本轮串行下载")
    return settings


def protect_generated_config(root, progress):
    """A resume must not erase subsequently edited fees, calendars, or strategy settings."""
    for name, fingerprint in progress.get("generated_config_sha256", {}).items():
        path = Path(root) / name
        if not path.is_file() or file_sha256(path) != fingerprint:
            raise ResearchError(
                f"{name} 已由使用者修改；停止自动再生成，保留参数。可直接运行 validate-data/backtest"
            )


def acquire_month(config_path, phase="all", request_limit=None):
    settings = acquisition_config(config_path)
    root = Path(settings["root"])
    budget = SpaceBudget(settings["budget"])
    budget.check(root, reserve=1024 * 1024)
    downloader = PublicDownloader(
        root, budget, timeout=settings.get("request_timeout", 40),
        download_allowance=settings.get("download_allowance"),
    )
    progress_path = root / "progress.json"
    progress = (
        json.loads(progress_path.read_text())
        if progress_path.exists()
        else {"daily": {}, "minute": {}, "errors": []}
    )
    protect_generated_config(root, progress)
    signature = digest(
        {k: settings[k] for k in ("month", "warmup_days", "products") if k in settings}
    )
    if progress.get("scope_hash", signature) != signature:
        raise ResearchError("已保存下载任务的月份/品种/预热范围不同，请另建输入目录")
    progress["scope_hash"] = signature
    progress["status"] = "running"
    progress.pop("last_error", None)

    def save():
        progress["storage"] = budget.check(root)
        progress["updated_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_json(progress_path, progress, budget)

    def quota():
        return request_limit is None or downloader.request_count < request_limit

    try:
        catalog, _ = downloader.fetch(
            CATALOG_URL,
            "raw/catalog.json",
            cap=settings.get("catalog_max_bytes", 1024**3),
            timeout=settings.get("catalog_timeout", 1200),
        )
        holiday, _ = downloader.fetch(
            HOLIDAYS_URL, "raw/holidays.json", cap=1024 * 1024
        )
        calendar = monthly_calendar(
            settings["month"],
            json.loads(holiday.read_text()),
            settings.get("warmup_days", 5),
        )
        progress["stage"] = "daily" if phase != "catalogue" else "catalogue"
        contracts, exclusions, indexed_products = catalogue_contracts(
            catalog, calendar, settings["month"], settings.get("products")
        )
        if not contracts:
            raise ResearchError("公开目录在指定月份/范围内没有可用真实合约")
        atomic_json(
            root / "catalogue_audit.json",
            {
                "source": CATALOG_URL,
                "sha256": file_sha256(catalog),
                "contracts": len(contracts),
                "products": sorted(
                    {r["exchange"] + "." + r["product"] for r in contracts}
                ),
                "indexed_products": indexed_products,
                "excluded": exclusions,
                "historical_universe_verified": False,
            },
            budget,
        )
        if phase == "catalogue":
            progress["status"] = "catalogue_completed"
            save()
            return progress
        daily = []
        for i, contract in enumerate(contracts, 1):
            symbol = contract["provider_symbol"]
            url = edb_url(
                symbol,
                86400,
                calendar["trading_days"][0] + " 00:00:00",
                month_bounds(settings["month"])[1].isoformat() + " 00:00:00",
            )
            path = root / f"raw/daily/{symbol}.csv"
            if not quota() and not path.with_suffix(".csv.receipt.json").exists():
                progress["status"] = "paused_request_budget"
                save()
                return progress
            try:
                path, cached = downloader.fetch(url, f"raw/daily/{symbol}.csv")
                rows = normalize_daily(path, contract, calendar)
                daily.extend(rows)
                contract["listed"] = min((r["trading_day"] for r in rows), default=None)
                progress["daily"][symbol] = {
                    "rows": len(rows),
                    "sha256": file_sha256(path),
                }
                if not cached:
                    print(
                        f"日线 {i}/{len(contracts)} {symbol}：{len(rows)} 条",
                        flush=True,
                    )
            except ResearchError as exc:
                if "预算" in str(exc) or "余量" in str(exc) or "校验" in str(exc):
                    raise
                progress["errors"].append(
                    {"phase": "daily", "contract": symbol, "error": str(exc)}
                )
                progress["daily"][symbol] = {"error": str(exc)}
            save()
        if not daily:
            raise ResearchError("指定月份没有任何真实日线，不能自行改用其他月份")
        write_parquet(root / "daily.parquet", daily, budget)
        metadata = {"source": CATALOG_URL, "contracts": contracts}
        atomic_json(root / "metadata.json", metadata, budget)
        atomic_json(root / "calendar.json", calendar, budget)
        template = Path(__file__).parent / "examples" / "config.json"
        cfg = json.loads(template.read_text())
        cfg.update(metadata=metadata, calendar=calendar)
        cfg["data"].update(
            sources=[],
            daily_sources=[{"format": "parquet", "path": str(root / "daily.parquet")}],
        )
        observations = [DailyObservation(**r) for r in daily]
        data = Dataset([], cfg, daily=observations)
        start, end = month_bounds(settings["month"])
        pool, rejected = [], []
        for day in calendar["trading_days"]:
            if start.isoformat() <= day < end.isoformat():
                picked, excluded = data.pool(day)
                pool.extend({k: v for k, v in r.items() if k != "meta"} for r in picked)
                rejected.extend(excluded)
        atomic_json(root / "daily_selection.json", pool, budget)
        atomic_json(root / "selection_exclusions.json", rejected, budget)
        expected = minute_plan(pool, calendar, settings.get("warmup_days", 5))
        atomic_json(root / "minute_plan.json", expected, budget)
        if phase == "daily":
            progress["status"] = "daily_completed"
            save()
            return progress
        sources, coverage = [], []
        by_key = defaultdict(list)
        progress["stage"] = "minute"
        for row in expected:
            by_key[row["contract"]].append(row)
        for i, (key, requests) in enumerate(sorted(by_key.items()), 1):
            contract = data.metadata.get(key, end.isoformat())
            days = sorted(r["trading_day"] for r in requests)
            cal = data.calendar
            beginning = cal.bounds(days[0], contract)[0].strftime("%Y-%m-%d %H:%M:%S")
            ending = cal.bounds(days[-1], contract)[1].strftime("%Y-%m-%d %H:%M:%S")
            symbol = contract["provider_symbol"]
            url = edb_url(symbol, 60, beginning, ending)
            path = root / f"raw/minute/{symbol}.csv"
            if not quota() and not path.with_suffix(".csv.receipt.json").exists():
                progress["status"] = "paused_request_budget"
                save()
                return progress
            try:
                path, cached = downloader.fetch(url, f"raw/minute/{symbol}.csv")
                rows, removed = normalize_minutes(path, contract, calendar, requests)
                partition = root / "minutes" / (key + ".parquet")
                if rows:
                    write_parquet(partition, rows, budget)
                    sources.append({"format": "parquet", "path": str(partition)})
                progress["minute"][symbol] = {
                    "rows": len(rows),
                    "removed": removed,
                    "raw_sha256": file_sha256(path),
                    "partition_sha256": file_sha256(partition) if rows else None,
                }
                coverage.append(
                    {
                        "contract": key,
                        "rows": len(rows),
                        "dates": sorted({r["trading_day"] for r in rows}),
                        "removed": removed,
                        "requested_days": len(days),
                    }
                )
                if not cached:
                    print(
                        f"分钟 {i}/{len(by_key)} {symbol}：日盘保留 {len(rows)} 条",
                        flush=True,
                    )
            except ResearchError as exc:
                if "预算" in str(exc) or "余量" in str(exc) or "校验" in str(exc):
                    raise
                progress["errors"].append(
                    {"phase": "minute", "contract": symbol, "error": str(exc)}
                )
                progress["minute"][symbol] = {"error": str(exc)}
            save()
        cfg["data"].update(
            sources=sources,
            daily_sources=[{"format": "parquet", "path": "daily.parquet"}],
            expected_contract_days=expected,
        )
        for source in sources:
            source["path"] = os.path.relpath(source["path"], root)
        cfg.update(metadata="metadata.json", calendar="calendar.json")
        cfg["strategy"]["approximate_vwap"] = True
        cfg["strategy"]["fixed_ticks"] = {
            r["product"]: {"stop_loss_ticks": None, "take_profit_ticks": None}
            for r in contracts
        }
        cfg["splits"] = settings["splits"]
        cfg["storage"] = {
            "shared_root": "datasets",
            "compact_results": True,
            "audit_export_normalized": False,
            "budget": settings["budget"],
        }
        cfg["experiments"]["max_runs"] = settings.get("experiment_budget", 6)
        cfg["research_blockers"] = [
            "公开最新目录未证明历史全合约池完整；窗口内首次日线仅为上市日期代理"
        ]
        atomic_json(root / "config.json", cfg, budget)
        progress["generated_config_sha256"] = {
            name: file_sha256(root / name)
            for name in ("config.json", "metadata.json", "calendar.json")
        }
        atomic_json(
            root / "acquisition_summary.json",
            {
                "month": settings["month"],
                "provenance": "REAL_EDB_PUBLIC",
                "coverage": coverage,
                "daily_rows": len(daily),
                "pool_days_products": len(pool),
                "minute_rows": sum(r["rows"] for r in coverage),
                "approximate_vwap": True,
                "full_market_ranking_verified": False,
                "storage": budget.check(root),
                "errors": progress["errors"],
                "documentation": DOCUMENTATION,
            },
            budget,
        )
        progress["status"] = (
            "completed_with_gaps" if progress["errors"] else "data_download_completed"
        )
        save()
        return progress
    except Exception as exc:
        progress["status"] = "stopped"
        progress["last_error"] = str(exc)
        # A small progress record can still be written if the budget itself caused the stop.
        try:
            atomic_json(progress_path, progress, budget)
        except (ResearchError, OSError):
            pass
        raise
    finally:
        if downloader.http is not None:
            downloader.http.close()


def report_acquisition(root, audit):
    """Coverage/capacity report, deliberately contains no strategy return estimates."""
    from .experiments import code_identity, dependencies
    from .storage import directory_bytes

    root, audit = Path(root), Path(audit)
    summary = json.loads((root / "acquisition_summary.json").read_text())
    quality = json.loads((audit / "data_quality.json").read_text())
    pool = json.loads((root / "daily_selection.json").read_text())
    candidates = json.loads((audit / "daily_candidates.json").read_text())
    excluded = json.loads((audit / "ranking_exclusions.json").read_text())
    settings = json.loads((root / "acquire.json").read_text())
    policy = acquisition_config(root / "acquire.json")["budget"]
    report_budget = SpaceBudget(policy)
    report_budget.check(root, reserve=40 * 1024 * 1024)
    inventory = [
        {
            "path": str(p.relative_to(root)),
            "bytes": p.stat().st_size,
            "sha256": file_sha256(p),
        }
        for p in [root / "daily.parquet"] + sorted((root / "minutes").glob("*.parquet"))
    ]
    by_group = defaultdict(
        lambda: {"products": set(), "contracts": set(), "bars": 0, "dates": set()}
    )
    covered_products = {
        (m["symbol"] + "." + m["exchange"]): m
        for m in json.loads((root / "metadata.json").read_text())["contracts"]
    }
    for row in summary["coverage"]:
        if not row["rows"]:
            continue
        meta = covered_products[row["contract"]]
        group = by_group[meta["group"]]
        group["products"].add(meta["exchange"] + "." + meta["product"])
        group["contracts"].add(row["contract"])
        group["bars"] += row["rows"]
        group["dates"].update(row["dates"])
    groups = {
        k: {
            **v,
            "products": sorted(v["products"]),
            "contracts": sorted(v["contracts"]),
            "dates": sorted(v["dates"]),
        }
        for k, v in sorted(by_group.items())
    }
    raw_minutes = directory_bytes([root / "raw" / "minute"])
    minute_bytes = directory_bytes([root / "minutes"])
    bars = summary["minute_rows"]
    validation = settings["splits"]["validation"]
    validation_candidates = [
        r for r in candidates if validation["start"] <= r["date"] <= validation["end"]
    ]
    candidate_counts = []
    for k in [1, 2, 3, 4, 5, 8, 10, "ALL"]:
        for group in ["commodity", "financial"]:
            eligible = [
                r
                for r in validation_candidates
                if r["group"] == group and (k == "ALL" or r["rank"] <= k)
            ]
            candidate_counts.append(
                {
                    "k": k,
                    "group": group,
                    "long_candidate_days": sum(
                        r["direction"] == "LONG" for r in eligible
                    ),
                    "short_candidate_days": sum(
                        r["direction"] == "SHORT" for r in eligible
                    ),
                    "validation_window": validation,
                    "interpretation": "排名候选数量；不是入场信号、成交或收益验证",
                }
            )
    facts = {
        "month": summary["month"],
        "provenance": "REAL_EDB_PUBLIC",
        "groups": groups,
        "parquet_inventory": inventory,
        "storage": summary["storage"],
        "raw_minute_bytes_including_night_and_receipts": raw_minutes,
        "day_minute_parquet_bytes": minute_bytes,
        "parquet_bytes_per_day_bar": minute_bytes / bars if bars else None,
        "quality_errors": len(quality["errors"]),
        "ranking_exclusions": Counter(
            reason
            for r in excluded
            for reason in r.get("reasons", [r.get("reason", "unknown")])
        ),
        "research_days": sorted({r["date"] for r in pool}),
        "candidate_count": len(candidates),
        "selection_basis": "previous trading-day daily close_oi",
        "assumptions": {
            "approximate_vwap": True,
            "timestamp": "start",
            "volume": "incremental lots",
            "minute_oi": "close_oi snapshot",
            "session_open_oi": "first day-minute open_oi",
            "warmup_cap_days": settings["warmup_days"],
        },
        "backtest_completed": False,
        "topk_profit_conclusion": None,
        "topk_candidate_coverage_validation_only": candidate_counts,
        "formal_backtest_blockers": quality["configuration_gaps"],
        "dependencies": dependencies(),
        **code_identity(),
    }
    import plotly.graph_objects as go

    coverage_chart = go.Figure()
    for group in ["commodity", "financial"]:
        days = facts["research_days"]
        available = Counter(r["date"] for r in pool if r["group"] == group)
        ranked = Counter(r["date"] for r in candidates if r["group"] == group)
        coverage_chart.add_scatter(
            x=days, y=[available[d] for d in days], name=group + "：前日选定合约数"
        )
        coverage_chart.add_scatter(
            x=days, y=[ranked[d] for d in days], name=group + "：有效方向排名品种数"
        )
    coverage_chart.update_layout(
        title="2026-09 日盘数据覆盖（候选数量，非收益验证）", template="plotly_white"
    )
    coverage_chart.write_html(root / "data_coverage.html", include_plotlyjs=True)
    k_chart = go.Figure()
    for group in ["commodity", "financial"]:
        rows = [r for r in candidate_counts if r["group"] == group]
        k_chart.add_bar(
            x=[str(r["k"]) for r in rows],
            y=[r["long_candidate_days"] + r["short_candidate_days"] for r in rows],
            name=group,
        )
    k_chart.update_layout(
        title="验证段 Top-K 候选数量（不是交易次数或最佳K判断）",
        template="plotly_white",
        xaxis_type="category",
        barmode="group",
    )
    k_chart.write_html(root / "topk_candidate_counts.html", include_plotlyjs=True)
    facts["storage"] = report_budget.check(root)
    atomic_json(root / "monthly_validation.json", facts, report_budget)
    lines = [
        "# 2026年9月公开真实期货数据接入与容量报告",
        "",
        "本次完成数据工程接入和开盘排名审计。没有策略收益验证、最佳K结论或实盘验证。",
        "",
        f"研究月份：{summary['month']}（Asia/Shanghai，左闭右开）；预热最多{settings['warmup_days']}个交易日。",
        f"原始日线 {summary['daily_rows']:,} 条；保留真实日盘分钟 {bars:,} 根。",
        "",
        "| 分组 | 品种数 | 真实合约数 | 日盘分钟数（含预热） |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, group in groups.items():
        lines.append(
            f"| {name} | {len(group['products'])} | {len(group['contracts'])} | {group['bars']:,} |"
        )
    for name, group in groups.items():
        lines.append(
            "\n" + name + " 实际分钟覆盖品种：" + ", ".join(group["products"]) + "。"
        )
    lines += [
        "",
        "## 数据范围与口径",
        "",
        "日线取所有本轮目录内真实合约的前日收市持仓量/成交量，锁定当日代表合约；未选交割合约不下载完整分钟。",
        "当前目录是供应商最新目录，并非当时的历史目录；首次出现的完整日线只能作为保守上市日期代理。不能据此声称全市场历史合约池已完全核实。",
        "EDB 日线时间戳实测为上海交易日零点。分钟为开始标记，成交量为增量手数，持仓量为分钟末时点；第一根日盘 open_oi 用作开盘持仓快照。",
        "没有成交额字段，配置显式开启典型价量权近似均价；不能作为原策略精确VWAP验证。跨日范围接口附带夜盘原始记录，标准化时排除，未加入夜盘信号/指标。",
        "日历使用官方 SDK 引用的供应商节假日表，9月25日休市；最新日盘时段尚未逐交易所核实历史变更，calendar.verified=false。",
        "缺失不填充，零量分钟不可成交；换月预热用新合约自身历史。具体每日合约、剔除及排名见 daily_selection.json、selection_exclusions.json 及数据审计目录。",
        "",
        f"研究日期：{', '.join(facts['research_days'])}。数据校验错误 {facts['quality_errors']} 条；候选排名记录 {len(candidates)} 条。",
        "排名数据不足等剔除："
        + json.dumps(facts["ranking_exclusions"], ensure_ascii=False),
        "",
        "## 实测空间",
        "",
        f"日盘分钟 Parquet：{minute_bytes / 1024**2:.2f} MiB，约 {minute_bytes / bars if bars else 0:.2f} 字节/根。",
        f"原始分钟及校验凭据（含接口返回的夜盘）：{raw_minutes / 1024**2:.2f} MiB。",
        f"本轮预算目录实际占用：{facts['storage']['used_bytes'] / 1024**2:.2f} MiB；磁盘剩余：{facts['storage']['free_bytes'] / 1024**3:.2f} GiB。",
        "预算包括原始目录、未完成临时响应、Parquet、行情共享对象及本月结果。未删除以前的研究数据，也未扩容。每次实验只保存共享数据版本、分区及SHA256引用。",
        "以上是本月实测，不能当作多年数据或Tick容量承诺。",
        "覆盖图 data_coverage.html；验证段K候选数量图 topk_candidate_counts.html。数量统计没有使用锁定测试选择K，也不是收益或成交质量比较。",
        "",
        "## 收益研究阻塞",
        "",
        "当前仍缺经过历史核实的上市/时段信息、成交额换算系数、按生效日手续费和平今费、保证金，以及有效固定止损止盈配置。正式回测明确失败，不输出伪造零收益或最佳K。",
        "按月训练/验证/锁定测试分割只是接线研究初值，验证交易日少于20日，不能据此宣布稳定参数。训练ATR候选仍需审阅并冻结；不会从最终测试波动反推跳数。",
        "",
        "## 可复现",
        "",
        f"代码指纹：`{facts['code_hash']}`；数据指纹见审计 data_quality.json；分区文件指纹与依赖版本见 monthly_validation.json。",
        "运行命令、分阶段下载与续传说明见 research/README.md。公开源文档："
        + DOCUMENTATION,
        "",
        "工程测试、数据接入和分钟级历史回测是不同验收层次；本轮未完成样本外收益、tick、模拟交易或实盘验证。",
    ]
    path = root / "monthly_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
