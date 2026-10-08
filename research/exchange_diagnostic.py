"""Extend frozen diagnostics with archived CZCE/GFEX/CFFEX rules.

Only execution qualifications change. The original pool, training calibration,
signal rules and capital settings are retained. This is never formal validation.
"""

import copy
import csv
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .calendar import MINUTE, Calendar, at
from .config import ResearchError, digest, read_config
from .data import Metadata, file_sha256
from .execution import fee
from .execution_parameters import ExecutionParameters
from .experiments import code_identity
from .reporting import write_csv, write_json
from .storage import SpaceBudget


def number(value):
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ResearchError("官方数值为空或格式无效") from exc
    if not result.is_finite() or result < 0:
        raise ResearchError("官方数值为空、负数或非有限数")
    return result


def charge(value, style):
    """Exchange convention: fixed CNY/lot; ratio quoted per ten thousand."""
    if style not in {"绝对值", "比例值"}:
        raise ResearchError("未知交易所收费方式，不猜测换算")
    return {
        "mode": "fixed" if style == "绝对值" else "rate",
        "value": float(number(value) / (1 if style == "绝对值" else 10000)),
    }


def czce_parameters(text, day):
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or f"({day})" not in lines[0] or "期货结算参数表" not in lines[0]:
        raise ResearchError("郑商所结算参数日期或表名错配")
    reader = csv.DictReader(lines[1:], delimiter="|")
    required = {
        "合约代码",
        "交易保证金率(%)",
        "交易手续费",
        "手续费收取方式",
        "日内平今仓交易手续费",
        "交易限额",
    }
    if not required <= set(reader.fieldnames or []):
        raise ResearchError("郑商所参数字段缺失")
    result = {}
    for raw in reader:
        raw = {k: v.strip() if v is not None else "" for k, v in raw.items()}
        symbol = raw["合约代码"]
        if not re.fullmatch(r"[A-Z]+\d{3,4}", symbol):
            continue
        result[symbol] = {
            "margin_rate": float(number(raw["交易保证金率(%)"]) / 100),
            "fees": {
                "open": charge(raw["交易手续费"], raw["手续费收取方式"]),
                "close_yesterday": charge(raw["交易手续费"], raw["手续费收取方式"]),
                "close_today": charge(
                    raw["日内平今仓交易手续费"], raw["手续费收取方式"]
                ),
            },
            "daily_open_limit": int(number(raw["交易限额"]))
            if raw["交易限额"]
            else None,
            "raw": raw,
        }
    if not result:
        raise ResearchError("郑商所参数未包含真实交割合约")
    return result


def cffex_parameters(text, day):
    lines = text.lstrip("\ufeff").splitlines()
    if (
        not lines
        or day.replace("-", "") not in lines[0]
        or "期货合约结算业务参数表" not in lines[0]
    ):
        raise ResearchError("中金所结算参数日期或表名错配")
    reader = csv.DictReader(lines[1:])
    required = {
        "期货合约",
        "交易手续费标准",
        "平今仓收取率",
        "合约多头保证金标准",
        "合约空头保证金标准",
    }
    if not required <= set(reader.fieldnames or []):
        raise ResearchError("中金所参数字段缺失")
    result = {}
    for raw in reader:
        symbol = raw["期货合约"].strip()
        if not re.fullmatch(r"[A-Z]+\d{4}", symbol):
            continue  # Footer notes are not incomplete futures-contract rows.
        item = raw["交易手续费标准"].strip()
        if re.fullmatch(r"万分之[0-9.]+", item):
            opening = {
                "mode": "rate",
                "value": float(number(item.removeprefix("万分之")) / 10000),
            }
        elif re.fullmatch(r"[0-9.]+元/手", item):
            opening = {
                "mode": "fixed",
                "value": float(number(item.removesuffix("元/手"))),
            }
        else:
            raise ResearchError("未知中金所手续费单位")
        today = number(raw["平今仓收取率"].removesuffix("%")) / 100
        rates = [
            number(raw[f"合约{side}头保证金标准"].removesuffix("%")) / 100
            for side in ("多", "空")
        ]
        result[symbol] = {
            "margin_rate": float(max(rates)),
            "fees": {
                "open": opening,
                "close_yesterday": copy.deepcopy(opening),
                "close_today": {
                    **opening,
                    "value": float(Decimal(str(opening["value"])) * today),
                },
            },
            "raw": raw,
        }
    return result


def gfex_parameters(document, day):
    if str(document.get("code")) != "0" or document.get("param", {}).get(
        "trade_date"
    ) != [day.replace("-", "")]:
        raise ResearchError("广期所查询失败或历史日期错配；响应time不是历史发布时间")
    result = {}
    for raw in document["data"]:
        if raw.get("varietyOrder") != "lc":
            continue
        if number(raw["openFee"]) != number(raw["shortOpenFee"]):
            raise ResearchError("短线开仓与普通开仓费用不同，须核实开仓收费时点")
        result[raw["contractId"]] = {
            "margin_rate": float(
                max(number(raw["specBuyRate"]), number(raw["specSellRate"]))
            ),
            "fees": {
                "open": charge(raw["openFee"], raw["style"]),
                "close_yesterday": charge(raw["offsetFee"], raw["style"]),
                "close_today": charge(raw["shortOffsetFee"], raw["style"]),
            },
            "raw": raw,
        }
    return result


class PublicArchive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.receipts = {}
        for line in (self.root / "probe_receipts.jsonl").read_text().splitlines():
            row = json.loads(line)
            if row.get("path") and row.get("status") == 200 and not row.get("error"):
                self.receipts[Path(row["path"]).name] = row

    def read(self, name, host):
        path = self.root / name
        receipt = self.receipts.get(name)
        if not receipt or path.parent != self.root:
            raise ResearchError("缺少完整公开归档：" + name)
        if urlsplit(receipt.get("final_url", receipt["url"])).hostname != host:
            raise ResearchError("公开资料重定向到错误来源")
        if (
            receipt.get("credential_used") is not False
            or file_sha256(path) != receipt["sha256"]
        ):
            raise ResearchError("公开资料凭据或SHA256错配")
        value = path.read_bytes()
        try:
            value = value.decode("utf-8-sig")
        except UnicodeDecodeError:
            value = value.decode("gb18030")
        return value, {
            "path": str(path),
            **{k: receipt[k] for k in ("url", "sha256", "checked_utc", "bytes")},
        }


def extend_diagnostic(config, source_dir, output):
    cfg = read_config(config)
    if cfg.get("price_replay") or cfg.get("execution", {}).get("mode") != "diagnostic":
        raise ResearchError(
            "扩展必须使用冻结的诊断配置，不能从价格回放或正式配置绕过准备"
        )
    metadata = Metadata(cfg["metadata"])
    ExecutionParameters(
        cfg, metadata
    )  # Validate the original freeze before adding any rule.
    q = copy.deepcopy(cfg["execution"]["qualification"])
    if q["schema"] != 1:
        raise ResearchError("扩展只从原schema=1诊断版本准备，不重复叠加或改写旧结果")
    baseline = {
        k: digest(cfg[k])
        for k in (
            "metadata",
            "calendar",
            "strategy",
            "risk",
            "splits",
            "calibration_snapshot",
        )
    }
    budget = SpaceBudget(cfg["storage"]["budget"])
    output = Path(output).resolve()
    budget.check(output, reserve=8 * 1024**2)
    output.mkdir(parents=True, exist_ok=True)
    archive, calendar = PublicArchive(source_dir), Calendar(cfg["calendar"])
    spec_path = Path(source_dir) / "reviewed_specifications.json"
    specifications = json.loads(spec_path.read_text())
    if specifications.get("kind") != "BOUNDED_DIAGNOSTIC_SPECIFICATION_REVIEW":
        raise ResearchError("缺少明确区分规范证据和历史延续假设的复核文件")
    targets = specifications["contracts"]
    specs = {}
    references = []
    for key, spec in targets.items():
        base = metadata.get(key, cfg["splits"]["validation"]["start"])
        if not base or any(
            base[f] != spec[f] for f in ("tick_size", "value_per_price")
        ):
            raise ResearchError(
                "复核规格与冻结训练报价步长不一致，不能直接沿用固定保护"
            )
        if spec["basis"] == "dated_official_specification_continuity_assumption":
            text, source = archive.read(spec["document"], spec["host"])
            plain = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", text)))
            for excerpt in spec["required_excerpts"]:
                if re.sub(r"\s+", "", excerpt) not in plain:
                    raise ResearchError("已复核规范引用在归档原文中不存在：" + key)
            if (
                at(spec["published_date"], "23:59:59")
                >= calendar.bounds(cfg["splits"]["validation"]["start"], base)[0]
            ):
                raise ResearchError("复核规范在验证开盘时尚未公开")
            references.append(source)
        elif spec["basis"] == "supplier_specification_continuity_assumed":
            if not spec.get("historical_applicability_assumed") or not base.get(
                "source"
            ):
                raise ResearchError("供应商规范历史适用假设必须显式声明并保留来源")
            source = {
                "url": base["source"],
                "source_asof": base.get("source_asof"),
                "historical_applicability_verified": False,
            }
        else:
            raise ResearchError("未知规格复核类型")
        specs[key] = {**spec, "source": source}
    days = [
        d
        for d in calendar.days
        if q["validation_window"]["start"] <= d <= q["validation_window"]["end"]
    ]
    schedules = []
    for day in ("2026-08-28", "2026-09-14", "2026-09-21"):
        text, source = archive.read(
            "cffex_settlement_" + day.replace("-", "") + ".csv", "www.cffex.com.cn"
        )
        schedules.append((day, cffex_parameters(text, day), source))
    lc_notice, lc_source = archive.read(
        "gfex_lc_september_fee_order_notice.html", "www.gfex.com.cn"
    )
    lc_notice = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", lc_notice)))
    required_notice = (
        "2026年9月22日交易时起",
        "LC2701",
        "最小开仓下单数量调整为2手",
        "交易手续费标准调整为成交金额的万分之零点八",
        "日内平今仓交易手续费标准调整为成交金额的万分之零点八",
        "单日开仓量分别不得超过800手",
    )
    if any(term not in lc_notice for term in required_notice):
        raise ResearchError("LC生效日期/合约/最小手数公告不匹配")
    new_rules, arithmetic, rejected, raw_rows = [], [], {}, []
    for day in days:
        previous = calendar.previous(day)
        for key, spec in specs.items():
            base = metadata.get(key, day)
            symbol = base["symbol"]
            exchange = base["exchange"]
            opening, closing = calendar.bounds(day, base)
            try:
                source_day, basis = previous, "previous_close_archive"
                extra = []
                if exchange == "CZCE":
                    name = (
                        "czce_static_clear_20260911_retry.txt"
                        if previous == "2026-09-11"
                        else "czce_clear_" + previous.replace("-", "") + ".txt"
                    )
                    text, source = archive.read(name, "www.czce.com.cn")
                    parameter = czce_parameters(text, previous)[symbol]
                elif exchange == "CFFEX":
                    source_day, rows, source = max(
                        (s for s in schedules if s[0] <= previous), key=lambda s: s[0]
                    )
                    parameter, basis = rows[symbol], "dated_schedule_continuity"
                elif exchange == "GFEX":
                    if day < "2026-09-22":
                        raise ResearchError(
                            "LC9/22之前最小开仓手数历史依据未复核，保留拒绝"
                        )
                    name = "gfex_lc_settlement_" + previous.replace("-", "") + ".json"
                    text, source = archive.read(name, "www.gfex.com.cn")
                    parameter = gfex_parameters(json.loads(text), previous)[symbol]
                    # The new fee is announced in advance; do not wait for a later close table.
                    parameter = {
                        **parameter,
                        "fees": {
                            side: {"mode": "rate", "value": 0.00008}
                            for side in ("open", "close_today", "close_yesterday")
                        },
                    }
                    extra.append(lc_source)
                else:
                    raise ResearchError("仅支持本轮指定的CZCE/GFEX/CFFEX真实合约")
                rule = {
                    "contract": key,
                    "trading_day": day,
                    "source_date": source_day,
                    "source_basis": basis,
                    "available_at": at(source_day, "23:59:59").isoformat(),
                    "availability_basis": "dated_archive_assumed_available_by_day_end",
                    "margin_effective_at": at(source_day, "15:00").isoformat(),
                    "effective_from": opening.isoformat(),
                    "effective_to": (closing + MINUTE).isoformat(),
                    "tick_size": spec["tick_size"],
                    "value_per_price": spec["value_per_price"],
                    "specification_basis": spec["basis"],
                    "specification_source": spec["source"],
                    "historical_specification_verified": False,
                    "listed": base["listed"],
                    "expiry": base["expiry"],
                    "lifecycle_basis": "existing_supplier_dates_proxy_unchanged",
                    "margin_rate": parameter["margin_rate"],
                    "fees": parameter["fees"],
                    "min_open_lots": 2 if exchange == "GFEX" else 1,
                    "sources": [source, spec["source"], *extra],
                }
                if (
                    spec["basis"]
                    == "dated_official_specification_continuity_assumption"
                ):
                    rule["specification_available_at"] = at(
                        spec["published_date"], "23:59:59"
                    ).isoformat()
                    rule["specification_effective_from"] = at(
                        spec["effective_date"], "00:00"
                    ).isoformat()
                limit = 800 if exchange == "GFEX" else parameter.get("daily_open_limit")
                if limit:
                    rule["daily_open_limit"] = limit
                new_rules.append(rule)
                references.extend([source, *extra])
                q["rule_rejections"].pop(day + "/" + key, None)
                raw_rows.append(
                    {
                        "date": day,
                        "contract": key,
                        "reference_date": source_day,
                        "raw": parameter["raw"],
                    }
                )
                # Decimal calculation independent of the engine's fee function, at declared example prices.
                prices = (
                    (Decimal("100000"), Decimal("100020"))
                    if exchange != "CFFEX" or base["product"] in {"IC", "IM"}
                    else (Decimal("100"), Decimal("100.01"))
                )
                meta = {
                    **base,
                    "fees": [
                        {"effective_from": day, "effective_to": day, **rule["fees"]}
                    ],
                }
                expected, actual = {}, {}
                for side, price in (
                    ("open", prices[0]),
                    ("close_today", prices[1]),
                    ("close_yesterday", prices[1]),
                ):
                    item = rule["fees"][side]
                    expected[side] = float(
                        Decimal(str(item["value"]))
                        * (
                            price * Decimal(str(spec["value_per_price"]))
                            if item["mode"] == "rate"
                            else 1
                        )
                    )
                    actual[side] = fee(meta, day, side, float(price), 1)
                if any(abs(expected[k] - actual[k]) > 1e-9 for k in expected):
                    raise ResearchError("独立费用手算与撮合不一致")
                arithmetic.append(
                    {
                        "date": day,
                        "contract": key,
                        "example_prices_are_not_fills": True,
                        "expected": expected,
                        "engine": actual,
                        "sources": rule["sources"],
                        "passed": True,
                    }
                )
            except (ResearchError, KeyError) as exc:
                rejected[day + "/" + key] = [str(exc)]
    if not new_rules:
        raise ResearchError("没有可接入规则，已准备资料不能冒充成本执行完成")
    q.update(
        schema=2,
        rules=[*q["rules"], *new_rules],
        supplier_specification_continuity_assumed=True,
        rule_rejections={**q["rule_rejections"], **rejected},
        allowed_exchanges=sorted(
            set(q["allowed_exchanges"])
            | {
                metadata.get(r["contract"], r["trading_day"])["exchange"]
                for r in new_rules
            }
        ),
    )
    q["assumptions"] += [
        "新增交易所仅为诊断：按公开历史结算表的实际费率/保证金转换；无精确发布时间的日期表假设当日末已可获得，并延续到下一日盘。当前查询time不是历史发布时间。",
        "CZCE绝对值按元/手、比例值按成交额万分之X；CFFEX平今收取率是基础费率乘数，1000%为10倍；GFEX比例值按官网字段说明换算。",
        "CFFEX有日期合约规范延续适用；未取得有日期官方规格的CZCE/LC继续使用冻结供应商规格的历史延续假设，未声称完整历史规格已核实。所有上市/最后交易日期沿用原供应商代理，保持原池。",
        "LC9/22起最小开仓2手、每日开仓限额800手；更早最小开仓数量未核实，所以对应日拒绝执行；其他新增合约沿用标准最小1手假设。",
        "执行资料在观察验证信号后补查，用途为工程诊断，不能作为独立样本外收益或最佳K验证。",
    ]
    cfg["execution"].update(qualification=q, qualification_hash=digest(q))
    ExecutionParameters(cfg, metadata)
    if baseline != {k: digest(cfg[k]) for k in baseline}:
        raise ResearchError("扩展执行资料意外改动冻结研究配置")
    write_json(output / "diagnostic_config.json", cfg, budget)
    write_json(output / "execution_qualification.json", q, budget)
    write_json(output / "fee_arithmetic_checks.json", arithmetic, budget)
    write_json(output / "raw_parameter_review.json", raw_rows, budget)
    write_csv(
        output / "new_rule_coverage.csv",
        [
            {
                k: r[k]
                for k in (
                    "contract",
                    "trading_day",
                    "source_date",
                    "source_basis",
                    "margin_rate",
                    "min_open_lots",
                    "fees",
                )
            }
            for r in new_rules
        ],
        budget,
    )
    write_json(
        output / "preparation_manifest.json",
        {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "base_configuration": str(Path(config).resolve()),
            "base_configuration_hash": digest(read_config(config)),
            "configuration_hash": digest(cfg),
            "unchanged_basis_hashes": baseline,
            "new_rules": len(new_rules),
            "fee_checks": len(arithmetic),
            "rejected": rejected,
            "locked_test_read": False,
            "specification_review": {
                "path": str(spec_path.resolve()),
                "sha256": file_sha256(spec_path),
            },
            "sources": list({r["path"]: r for r in references if "path" in r}.values()),
            "storage": budget.check(output),
            **code_identity(),
        },
        budget,
    )
    return {
        "config": str(output / "diagnostic_config.json"),
        "new_rules": len(new_rules),
        "fee_checks": len(arithmetic),
        "rejected_contract_days": len(rejected),
    }
