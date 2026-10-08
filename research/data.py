import csv
import gzip
import hashlib
import io
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .calendar import MINUTE, Calendar, stamp
from .config import ResearchError, execution_gaps


@dataclass(frozen=True)
class Bar:
    datetime: object
    trading_day: str
    exchange: str
    symbol: str
    product: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float | None
    turnover: float | None = None
    session_open_oi: float | None = None
    tradable: bool = True
    limit_up: float | None = None
    limit_down: float | None = None
    provenance: str = "REAL_UNVERIFIED"

    @property
    def key(self):
        return f"{self.symbol}.{self.exchange}"

    @property
    def end(self):
        return self.datetime + MINUTE

    def wire(self):
        row = asdict(self)
        row["datetime"] = self.datetime.isoformat()
        return row


@dataclass(frozen=True)
class DailyObservation:
    """A completed trading-day observation, independent of minute coverage."""

    trading_day: str
    exchange: str
    symbol: str
    product: str
    volume: float
    open_interest: float | None
    complete: bool = True
    provenance: str = "REAL_UNVERIFIED"

    @property
    def key(self):
        return f"{self.symbol}.{self.exchange}"

    def wire(self):
        return asdict(self)


def file_sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def dataset_digest(bars, daily):
    """Stream canonical JSON; retain the legacy fingerprint when no daily input exists."""
    h = hashlib.sha256()
    if daily:
        h.update(b'{"bars": ')
    for records in [bars, daily] if daily else [bars]:
        if records is daily:
            h.update(b', "daily": ')
        h.update(b"[")
        for i, row in enumerate(records):
            if i:
                h.update(b", ")
            h.update(json_bytes(row.wire()))
        h.update(b"]")
    if daily:
        h.update(b"}")
    return h.hexdigest()


def json_bytes(value):
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


class Metadata:
    def __init__(self, cfg):
        self.records = defaultdict(list)
        for item in cfg["contracts"]:
            key = item["symbol"] + "." + item["exchange"]
            if not re.fullmatch(r"[A-Za-z]+[0-9]{3,4}\.[A-Z]+", key):
                raise ResearchError(f"必须使用真实交割合约，拒绝连续/期权代码：{key}")
            self.records[key].append(item)
        for records in self.records.values():
            records.sort(key=lambda x: x.get("effective_from") or "")
            starts = [r.get("effective_from") for r in records]
            if len(starts) != len(set(starts)):
                raise ResearchError("元数据生效日期重复")

    def get(self, key, day):
        records = [
            r
            for r in self.records.get(key, [])
            if r.get("effective_from") and r["effective_from"] <= day
        ]
        return records[-1] if records else None

    @staticmethod
    def eligible(item, day):
        return bool(
            item
            and item.get("listed")
            and item.get("expiry")
            and item["listed"] <= day <= item["expiry"]
            and item.get("trade_enabled", True)
        )


def source_rows(source, cutoff=None):
    path = Path(source["path"])
    if source["format"] == "csv":
        opener = gzip.open if path.suffix == ".gz" or source.get("compression") == "gzip" else open
        with opener(path, "rt", encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)
    elif source["format"] == "parquet":
        import pandas as pd

        try:
            filters = None
            if cutoff:
                from datetime import date

                import pyarrow as pa
                import pyarrow.parquet as pq

                schema = pq.read_schema(path)
                if "trading_day" not in schema.names:
                    raise ResearchError("Parquet 时间隔离需要显式 trading_day 字段")
                kind = schema.field("trading_day").type
                value = date.fromisoformat(cutoff) if pa.types.is_date(kind) else cutoff
                filters = [("trading_day", "<=", value)]
            yield from pd.read_parquet(path, filters=filters).to_dict("records")
        except ImportError as exc:
            raise ResearchError(
                "Parquet 需要 .venv/bin/python -m pip install '.[research-parquet]'"
            ) from exc
    elif source["format"] == "vnpy_sqlite":
        # Snapshot transaction, mode=ro; never import gateway/settings or change live database.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            query = "SELECT symbol,exchange,datetime,volume,turnover,open_interest,"
            query += (
                "open_price AS open,high_price AS high,low_price AS low,close_price AS close "
                "FROM dbbardata WHERE interval='1m'"
            )
            params = ()
            if cutoff:
                query += " AND datetime<?"
                params = (cutoff + " 18:00:00",)
            query += " ORDER BY datetime,symbol,exchange"
            for row in db.execute(query, params):
                yield dict(row)
    else:
        raise ResearchError("数据格式支持 csv/parquet/vnpy_sqlite")


def optional_number(row, name):
    value = row.get(name)
    if value is None or str(value).strip() in {"", "nan", "None", "NaT"}:
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ResearchError(f"{name} 非有限数或负数")
    return value


class Dataset:
    def __init__(self, bars, cfg, quality=None, daily=()):
        self.bars = tuple(sorted(bars, key=lambda x: (x.datetime, x.key)))
        self.cfg = cfg
        self.calendar = Calendar(cfg["calendar"])
        self.calendar.validate()
        self.metadata = Metadata(cfg["metadata"])
        self.by_contract = defaultdict(list)
        self.by_day = defaultdict(dict)
        for bar in self.bars:
            self.by_contract[bar.key].append(bar)
            self.by_day[(bar.trading_day, bar.key)][bar.datetime] = bar
        self.quality = quality or {}
        self.daily = tuple(sorted(daily, key=lambda x: (x.trading_day, x.key)))
        self.daily_by_day = {}
        for row in self.daily:
            identity = row.trading_day, row.key
            if identity in self.daily_by_day:
                raise ResearchError(f"重复合约日线：{identity}")
            self.daily_by_day[identity] = row
        self.fingerprint = dataset_digest(self.bars, self.daily)

    def until(self, day):
        """Optimization receives a physically truncated object, excluding locked data."""
        quality = dict(self.quality)
        quality["errors"] = [
            r
            for r in quality.get("errors", [])
            if not r.get("trading_day") or r["trading_day"] <= day
        ]
        quality["missing_minutes"] = [
            r for r in quality.get("missing_minutes", []) if r["date"] <= day
        ]
        result = Dataset(
            [b for b in self.bars if b.trading_day <= day],
            self.cfg,
            quality,
            daily=[r for r in self.daily if r.trading_day <= day],
        )
        quality["data_fingerprint"] = result.fingerprint
        quality["model_cutoff"] = day
        return result

    def training_fingerprint(self, day):
        return self.until(day).fingerprint

    def complete_previous(self, day, key):
        prev = self.calendar.previous(day)
        meta = self.metadata.get(key, prev or "")
        if not prev or not self.metadata.eligible(meta, prev):
            return None, "no_previous_eligible_contract"
        if self.cfg["data"].get("daily_sources") or self.daily:
            # Never substitute a different day or fall back to selected minute data.
            row = self.daily_by_day.get((prev, key))
            if not row or not row.complete:
                return None, "previous_daily_observation_missing_or_incomplete"
            if row.open_interest is None or row.open_interest <= 0:
                return None, "previous_close_oi_missing"
            return {"oi": row.open_interest, "volume": row.volume, "day": prev}, None
        try:
            expected = self.calendar.minutes(prev, meta, night=True)
        except ResearchError:
            return None, "missing_previous_night_calendar"
        rows = self.by_day.get((prev, key), {})
        if not expected or any(t not in rows for t in expected):
            return None, "previous_trading_day_incomplete"
        last = rows[expected[-1]]
        if last.open_interest is None or last.open_interest <= 0:
            return None, "previous_close_oi_missing"
        return {
            "oi": last.open_interest,
            "volume": sum(rows[t].volume for t in expected),
            "day": prev,
        }, None

    def pool(self, day):
        products, excluded = defaultdict(list), []
        for key in sorted(self.metadata.records):
            meta = self.metadata.get(key, day)
            if not self.metadata.eligible(meta, day):
                excluded.append(
                    {
                        "date": day,
                        "contract": key,
                        "reason": "not_listed_expired_or_metadata_missing",
                    }
                )
                continue
            prev, reason = self.complete_previous(day, key)
            if reason:
                excluded.append(
                    {
                        "date": day,
                        "contract": key,
                        "product": meta["product"],
                        "reason": reason,
                    }
                )
                continue
            products[(meta["group"], meta["product"])].append((key, meta, prev))
        selected = []
        for group_product, contracts in sorted(products.items()):
            ordered = sorted(
                contracts, key=lambda x: (-x[2]["oi"], -x[2]["volume"], x[0])
            )
            key, meta, prev = ordered[0]
            selected.append(
                {
                    "date": day,
                    "group": group_product[0],
                    "product": group_product[1],
                    "contract": key,
                    "previous_oi": prev["oi"],
                    "previous_volume": prev["volume"],
                    "selection_day": prev["day"],
                    "meta": meta,
                }
            )
            for other, _, _ in ordered[1:]:
                excluded.append(
                    {
                        "date": day,
                        "contract": other,
                        "product": meta["product"],
                        "reason": "lower_previous_close_oi",
                    }
                )
        return selected, excluded


def load_daily(cfg, quality, cutoff=None):
    daily, seen = [], set()
    metadata = Metadata(cfg["metadata"])
    for source in cfg["data"].get("daily_sources", []):
        if "cloud" in source:
            from .cloud_archive import materialize_source
            source = materialize_source(source)
        path = Path(source["path"])
        if not path.exists():
            quality["errors"].append(
                {"source": str(path), "reason": "daily_file_missing"}
            )
            continue
        item = {
            "path": str(path),
            "format": source["format"],
            "kind": "daily",
            "sha256": file_sha256(path),
        }
        counts = Counter()
        for line, raw in enumerate(source_rows(source, cutoff), 2):
            day = str(raw.get("trading_day", ""))[:10]
            if cutoff and day and day > cutoff:
                continue
            try:
                if day not in cfg["calendar"]["trading_days"]:
                    raise ResearchError("日线 trading_day 不在显式历史日历")
                key = raw["symbol"] + "." + raw["exchange"]
                meta = metadata.get(key, day)
                if not meta or raw["product"] != meta["product"]:
                    raise ResearchError("日线合约/品种元数据不符")
                if (day, key) in seen:
                    raise ResearchError("重复合约日线；禁止自动覆盖")
                volume = optional_number(raw, "volume")
                if volume is None:
                    raise ResearchError("日线成交量缺失")
                oi = optional_number(raw, "open_interest")
                provenance = str(
                    raw.get("provenance") or source.get("provenance", "REAL_UNVERIFIED")
                )
                if cfg.get("synthetic", False) != (provenance == "SYNTHETIC_TEST_ONLY"):
                    raise ResearchError("合成/真实日线标签与配置不符")
                if "complete" not in raw:
                    raise ResearchError(
                        "日线必须显式声明 complete，不能默认未收市数据已完整"
                    )
                complete = str(raw["complete"]).lower() in {"true", "1"}
                daily.append(
                    DailyObservation(
                        day,
                        raw["exchange"],
                        raw["symbol"],
                        raw["product"],
                        volume,
                        oi,
                        complete,
                        provenance,
                    )
                )
                seen.add((day, key))
                counts["rows"] += 1
                counts["oi_missing"] += oi is None
                counts["incomplete"] += not complete
            except (ValueError, TypeError, KeyError) as exc:
                quality["errors"].append(
                    {
                        "source": str(path),
                        "line": line,
                        "trading_day": day,
                        "reason": str(exc),
                    }
                )
        item["counts"] = dict(counts)
        quality["sources"].append(item)
    return daily


def load_data(cfg, cutoff=None):
    calendar, meta = Calendar(cfg["calendar"]), Metadata(cfg["metadata"])
    quality = {
        "errors": [],
        "warnings": [],
        "sources": [],
        "excluded_rows": [],
        "coverage": [],
        "approximations": [],
        "missing_minutes": [],
        "outside_day_session": 0,
    }
    daily = load_daily(cfg, quality, cutoff)
    bars, seen, previous = [], set(), {}
    for source in cfg["data"]["sources"]:
        if "cloud" in source:
            from .cloud_archive import materialize_source
            source = materialize_source(source)
        path = Path(source["path"])
        if not path.exists():
            quality["errors"].append({"source": str(path), "reason": "file_missing"})
            continue
        item = {"path": str(path), "format": source["format"]}
        if source["format"] != "vnpy_sqlite":
            item["sha256"] = file_sha256(path)
        quality["sources"].append(item)
        counts = Counter()
        last_dt = {}
        for line, raw in enumerate(source_rows(source, cutoff), 2):
            day = None
            try:
                row = dict(raw)
                dt = stamp(row["datetime"])
                if cfg["data"]["timestamp"] == "end":
                    dt -= MINUTE
                if dt.second or dt.microsecond:
                    raise ResearchError("非整分钟时间戳")
                key = row["symbol"] + "." + row["exchange"]
                day = str(row.get("trading_day") or calendar.infer_day(dt) or "")[:10]
                if cutoff and day and day > cutoff:
                    continue
                if not day:
                    quality["excluded_rows"].append(
                        {
                            "contract": key,
                            "datetime": dt.isoformat(),
                            "reason": "night_trading_day_missing",
                        }
                    )
                    continue
                if not row.get("trading_day"):
                    counts["day_session_trading_day_proxy"] += 1
                contract = meta.get(key, day)
                if not contract:
                    quality["excluded_rows"].append(
                        {
                            "contract": key,
                            "datetime": dt.isoformat(),
                            "reason": "contract_metadata_missing",
                        }
                    )
                    continue
                if row.get("product") and row["product"] != contract["product"]:
                    raise ResearchError("product 与独立元数据不符")
                if day not in calendar.days:
                    quality["excluded_rows"].append(
                        {
                            "contract": key,
                            "datetime": dt.isoformat(),
                            "reason": "not_in_calendar",
                        }
                    )
                    continue
                day_period = calendar.locate(dt, day, contract)
                if not day_period:
                    quality["outside_day_session"] += 1
                    if not cfg["strategy"][
                        "include_night_indicators"
                    ] and not contract.get("night_sessions"):
                        continue
                    if not calendar.locate(dt, day, contract, night=True):
                        quality["excluded_rows"].append(
                            {
                                "contract": key,
                                "datetime": dt.isoformat(),
                                "reason": "outside_historical_sessions",
                            }
                        )
                        continue
                numbers = {
                    k: float(row[k]) for k in ("open", "high", "low", "close", "volume")
                }
                if (
                    not all(math.isfinite(v) and v >= 0 for v in numbers.values())
                    or min(numbers[k] for k in ("open", "high", "low", "close")) <= 0
                ):
                    raise ResearchError("异常价格或成交量")
                if (
                    not numbers["low"]
                    <= min(numbers["open"], numbers["close"])
                    <= max(numbers["open"], numbers["close"])
                    <= numbers["high"]
                ):
                    raise ResearchError("OHLC 高低价不一致")
                oi, turnover = (
                    optional_number(row, "open_interest"),
                    optional_number(row, "turnover"),
                )
                if cfg["data"]["counter_mode"] == "cumulative":
                    counter_key = key, day
                    old = previous.get(counter_key)
                    if old is None:
                        if (
                            cfg["data"].get("cumulative_first_is_zero_based", False)
                            and day_period
                            and dt == calendar.bounds(day, contract)[0]
                        ):
                            old = (0, 0, dt - MINUTE)
                        else:
                            previous[counter_key] = (numbers["volume"], turnover, dt)
                            quality["excluded_rows"].append(
                                {
                                    "contract": key,
                                    "datetime": dt.isoformat(),
                                    "reason": "cumulative_initial_baseline_unknown",
                                }
                            )
                            continue
                    cumulative = numbers["volume"]
                    if (
                        dt != old[2] + MINUTE
                        or cumulative < old[0]
                        or (
                            turnover is not None
                            and old[1] is not None
                            and turnover < old[1]
                        )
                    ):
                        previous[counter_key] = (cumulative, turnover, dt)
                        quality["excluded_rows"].append(
                            {
                                "contract": key,
                                "datetime": dt.isoformat(),
                                "reason": "cumulative_reset_or_gap_unknown_increment",
                            }
                        )
                        continue
                    numbers["volume"] = cumulative - old[0]
                    increment = (
                        turnover - old[1]
                        if turnover is not None and old[1] is not None
                        else None
                    )
                    previous[counter_key] = (cumulative, turnover, dt)
                    turnover = increment
                if (key, dt) in seen:
                    raise ResearchError("重复合约分钟；禁止自动覆盖")
                if key in last_dt and dt <= last_dt[key]:
                    quality["warnings"].append(
                        {
                            "contract": key,
                            "datetime": dt.isoformat(),
                            "reason": "out_of_order_sorted",
                        }
                    )
                last_dt[key] = dt
                provenance = str(
                    row.get("provenance") or source.get("provenance", "REAL_UNVERIFIED")
                )
                if cfg.get("synthetic", False) != (provenance == "SYNTHETIC_TEST_ONLY"):
                    raise ResearchError("合成/真实数据标签与配置不符，禁止混用")
                bars.append(
                    Bar(
                        dt,
                        day,
                        row["exchange"],
                        row["symbol"],
                        contract["product"],
                        **numbers,
                        open_interest=oi,
                        turnover=turnover,
                        session_open_oi=optional_number(row, "session_open_oi"),
                        tradable=str(row.get("tradable", "true")).lower()
                        not in {"false", "0"},
                        limit_up=optional_number(row, "limit_up"),
                        limit_down=optional_number(row, "limit_down"),
                        provenance=provenance,
                    )
                )
                seen.add((key, dt))
                counts["rows"] += 1
                counts["zero_volume"] += numbers["volume"] == 0
                counts["oi_missing"] += oi is None
                counts["turnover_missing"] += turnover is None or (
                    turnover == 0 and numbers["volume"] > 0
                )
            except (ValueError, TypeError, KeyError, ResearchError) as exc:
                quality["errors"].append(
                    {
                        "source": str(path),
                        "line": line,
                        "trading_day": day,
                        "reason": str(exc),
                    }
                )
        item["counts"] = dict(counts)
    dataset = Dataset(bars, cfg, quality, daily=daily)
    for key, rows in sorted(dataset.by_contract.items()):
        quality["coverage"].append(
            {
                "contract": key,
                "product": rows[0].product,
                "rows": len(rows),
                "start": rows[0].datetime.isoformat(),
                "end": rows[-1].end.isoformat(),
                "trading_days": sorted({b.trading_day for b in rows}),
            }
        )
    expected_pairs = cfg["data"].get("expected_contract_days")
    expected_pairs = (
        {(r["trading_day"], r["contract"]) for r in expected_pairs}
        if expected_pairs is not None
        else None
    )
    for key in sorted(
        set(dataset.by_contract) | {r[1] for r in (expected_pairs or [])}
    ):
        for day in calendar.days:
            if cutoff and day > cutoff:
                continue
            if expected_pairs is not None and (day, key) not in expected_pairs:
                continue
            contract = meta.get(key, day)
            if not contract:
                continue
            rows = dataset.by_day.get((day, key), {})
            missing = [
                t.isoformat() for t in calendar.minutes(day, contract) if t not in rows
            ]
            quality["missing_minutes"].append(
                {
                    "date": day,
                    "contract": key,
                    "count": len(missing),
                    "minutes": missing,
                    "entire_day_absent": not rows,
                }
            )
    if cfg["strategy"]["approximate_vwap"]:
        quality["approximations"].append(
            "VWAP 全程使用 (H+L+C)/3 成交量加权，与精确模式独立；不会混用"
        )
    openings = [
        rows.get(calendar.bounds(day, meta.get(key, day))[0])
        for (day, key), rows in dataset.by_day.items()
        if meta.get(key, day)
    ]
    if any(b is not None and b.session_open_oi is None for b in openings):
        quality["approximations"].append(
            "部分开盘分钟无 session_open_oi，使用首分钟末持仓量代理，漏掉首分钟变化"
        )
    if daily:
        quality["daily_row_count"] = len(daily)
        quality["daily_selection_basis"] = (
            "上一完整交易日日线 close_oi；不要求未选合约的分钟数据"
        )
    products = {b.product for b in bars}
    quality["configuration_gaps"] = execution_gaps(cfg, products)
    quality["data_fingerprint"] = dataset.fingerprint
    quality["scope"] = (
        "SYNTHETIC_TEST_ONLY"
        if cfg.get("synthetic")
        else "仅上述真实合约；不是全市场排名"
    )
    quality["row_count"] = len(bars)
    quality["model_cutoff"] = cutoff
    return dataset


def export_csv(dataset, path):
    if not dataset.bars:
        return
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(dataset.bars[0].wire()))
                writer.writeheader()
                writer.writerows(b.wire() for b in dataset.bars)
