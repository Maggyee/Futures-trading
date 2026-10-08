"""Prepare a bounded, auditable SHFE/INE diagnostic execution profile.

It never changes the source metadata, universe, ranking, signals or locked split.
Only already completed official reports feed execution. Same-day trading tables
are an independent timing audit; their after-close publication is not an input.
"""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin

from .calendar import MINUTE, Calendar, at, stamp
from .config import ResearchError, digest, read_config
from .cost_review import PARAMETERS, fee_reference, parameter_rows
from .data import file_sha256, load_data
from .experiments import apply_calibration, code_identity, dependencies
from .reporting import write_csv, write_json
from .storage import SpaceBudget

TRADING_PARAMETERS = "https://www.shfe.com.cn/data/busiparamdata/future/ContractDailyTradeArgument{day}.dat"


def public_document(path, url, budget, fetch=False):
    """No credentials, no purchase, exact URL/hash receipts, bounded temporary writes."""
    path = Path(path)
    receipt = path.with_suffix(path.suffix + ".receipt.json")
    if not path.exists():
        if not fetch:
            raise ResearchError(
                f"缺少已归档官方资料：{path.name}；可使用 --fetch-official"
            )
        import httpx

        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        budget.check(partial, reserve=2 * 1024**2)
        size = 0
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(20, connect=8), follow_redirects=True
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as stream:
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > 2 * 1024**2:
                        raise ResearchError("官方资料响应超过2MiB上限，保留进度并停止")
                    budget.check(partial, reserve=len(chunk))
                    stream.write(chunk)
                    stream.flush()
        partial.replace(path)
        write_json(
            receipt,
            {
                "url": url,
                "sha256": file_sha256(path),
                "bytes": size,
                "requested_utc": datetime.now(timezone.utc).isoformat(),
                "credential_used": False,
            },
            budget,
        )
    if not receipt.exists():
        raise ResearchError(f"官方资料缺少校验凭据：{path}")
    saved = json.loads(receipt.read_text())
    if saved["url"] != url or saved["sha256"] != file_sha256(path):
        raise ResearchError(f"官方资料来源或SHA256改变：{path}")
    return path.read_text(), {"path": str(path.resolve()), **saved}


def contract_specifications(text, asof):
    """Use dated specifications available by the trading decision, not train end.

    A date without a publication clock becomes available at the end of that date.
    Its continuing applicability is an explicit research assumption. Undated
    current pages cannot establish historical applicability.
    """
    decision = stamp(asof) if isinstance(asof, datetime) else at(asof, "23:59:59")
    match = re.search(
        r"let\s+pageList\s*=\s*(\[.*?\])\s*(?:;?\s*</script>)", text, re.S
    )
    if not match:
        raise ResearchError("官方合约页没有可解析pageList")
    payload = re.sub(r",\s*([}\]])", r"\1", match[1].replace("\xa0", " "))
    rows = json.loads(payload)
    rows = [
        r
        for r in rows
        if r.get("data_standard")
        and at(r["data_standard"], "23:59:59") <= decision
    ]
    if not rows:
        raise ResearchError("合约规范缺少交易决策前日期证据，不能用未来或无日期的当前规范补证")
    row = sorted(rows, key=lambda r: r["data_standard"])[-1]
    tick_text, size_text, quote = (
        row["MinimumPriceFluctuation"],
        row["ContractSize"],
        row["PriceQuotation"],
    )
    tick_match, size_match = (
        re.search(r"[0-9.]+", tick_text),
        re.search(r"[0-9.]+", size_text),
    )
    if not tick_match:
        raise ResearchError("合约规范没有最小跳动数值")
    if row.get("ContractMultiplier"):
        multiplier = row["ContractMultiplier"]
        if "元" not in multiplier or "点" not in multiplier:
            raise ResearchError("非元/点合约乘数须人工核实")
        value = float(re.search(r"[0-9.]+", multiplier)[0])
    else:
        if not size_match:
            raise ResearchError("合约规范缺少交易单位")
        unit = next((u for u in ("千克", "吨", "克", "桶") if u in quote), None)
        if not unit or unit not in size_text or unit not in tick_text:
            raise ResearchError("交易单位/报价单位/跳动单位不匹配，不猜测换算")
        value = float(size_match[0])
    return {
        "tick_size": float(tick_match[0]),
        "value_per_price": value,
        "published_date": row["data_standard"],
        "available_at": at(row["data_standard"], "23:59:59").isoformat(),
        "effective_from": at(row["data_standard"], "23:59:59").isoformat(),
        "applicability_basis": "dated_official_specification_continuity_assumption",
        "raw_specification": row,
    }


def completed_parameter_sources(directory, day, exchange_by_product, budget):
    documents, sources = {}, []
    for table, template in PARAMETERS.items():
        text, reference = public_document(
            directory / (table + day.replace("-", "") + ".json"),
            template.format(day=day.replace("-", "")),
            budget,
        )
        documents[table] = json.loads(text)
        sources.append(reference)
    rows = parameter_rows(
        documents["ContractBaseInfo"], documents["Settlement"], exchange_by_product
    )
    available = max(
        stamp(datetime.strptime(d["update_date"], "%Y%m%d %H:%M:%S"))
        for d in documents.values()
    )
    if available.date().isoformat() != day or available < at(day, "15:00"):
        raise ResearchError("官方参数更新时间与收盘后口径不符，须另行核实")
    return {r["contract"]: r for r in rows if r["contract"]}, available, sources


def timing_check(day, key, previous, current):
    """Audit only: current after-close file is not a causal execution data source."""
    raw = current.get(key.split(".")[0])
    if not raw:
        return {
            "date": day,
            "contract": key,
            "matches": False,
            "reason": "intraday_table_contract_missing",
        }
    rates = previous["exchange_speculation_margin"]
    actual = {
        side: float(raw[field])
        for side, field in (
            ("long", "SPEC_LONGMARGINRATIO"),
            ("short", "SPEC_SHORTMARGINRATIO"),
        )
    }
    return {
        "date": day,
        "contract": key,
        "previous_settlement_margin": rates,
        "actual_intraday_margin": actual,
        "matches": rates == actual,
        "used_in_decisions": False,
    }


def prepare_diagnostic(config, calibration_path, output, tick_index=0, fetch=False):
    cfg = read_config(config)
    if cfg.get("execution", {}).get("mode", "formal") != "formal":
        raise ResearchError("prepare-diagnostic 的输入必须是原正式配置")
    policy = cfg.get("storage", {}).get("budget")
    if not policy:
        raise ResearchError("诊断接入必须设置包含原始、临时文件及结果的磁盘预算")
    budget = SpaceBudget(policy)
    root = Path(cfg.get("config_path", config)).resolve().parent
    output = Path(output).resolve()
    budget.check(output, reserve=32 * 1024**2)
    output.mkdir(parents=True, exist_ok=True)
    calibration = json.loads(Path(calibration_path).read_text())
    if (
        calibration.get("schema") != 2
        or calibration.get("locked_test_read") is not False
        or calibration["train_window"] != cfg["splits"]["train"]
    ):
        raise ResearchError("诊断只能使用同一冻结训练段的v2校准")
    # Validate the actual training fingerprint; no validation/test prices enter this step.
    data = load_data(cfg, cutoff=cfg["splits"]["train"]["end"])
    if calibration["train_data_hash"] != data.fingerprint:
        raise ResearchError("训练数据指纹已改变，须重新校准；不读取验证波动补算")
    cfg = apply_calibration(cfg, calibration, tick_index)
    cfg["execution"] = {"mode": "diagnostic"}
    calendar = Calendar(cfg["calendar"])
    directory = root / "source_docs" / "diagnostic"
    directory.mkdir(exist_ok=True)
    existing = root / "source_docs" / "exchanges"
    nav = (existing / "shfe_trading_sessions.html").read_text()
    links = {
        path.rsplit("/", 2)[-2].removesuffix("_f"): path
        for path in re.findall(r'href="([^"]+/[a-z]+_f/)"', nav)
    }
    products = {m["product"]: m["exchange"] for m in cfg["metadata"]["contracts"]}
    specifications, spec_rejections, sources, specification_texts = {}, {}, [], {}
    for product, exchange in sorted(products.items()):
        if exchange not in {"SHFE", "INE"}:
            continue
        try:
            url = urljoin("https://www.shfe.com.cn", links[product])
            text, receipt = public_document(
                directory / (product + "_spec.html"), url, budget, fetch
            )
            specification_texts[product] = (text, receipt)
            spec = contract_specifications(text, at(cfg["splits"]["validation"]["end"], "09:00"))
            originals = [
                r for r in cfg["metadata"]["contracts"] if r["product"] == product
            ]
            if any(
                r.get(f) != spec[f]
                for r in originals
                for f in ("tick_size", "value_per_price")
            ):
                raise ResearchError("官方规格与原校准元数据不同，禁止直接沿用固定跳数")
            spec["source"] = receipt
            specifications[product] = spec
            sources.append(receipt)
        except (ResearchError, ValueError, KeyError) as exc:
            spec_rejections[product] = str(exc)
        print(
            json.dumps(
                {
                    "phase": "contract_specification",
                    "product": product,
                    "qualified": product in specifications,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    # Reuse the original daily selection manifest; do not drop unavailable exchanges.
    selections = json.loads((root / "daily_selection.json").read_text())
    if isinstance(selections, dict):
        selections = selections["rows"]
    days = [
        d
        for d in calendar.days
        if cfg["splits"]["validation"]["start"]
        <= d
        <= cfg["splits"]["validation"]["end"]
    ]
    rules, rejections, checks = [], {}, []
    parameters = root / "source_docs" / "shfe_parameters"
    for day in days:
        previous_day = calendar.previous(day)
        previous, available, references = completed_parameter_sources(
            parameters, previous_day, products, budget
        )
        trading_text, receipt = public_document(
            directory / ("ContractDailyTradeArgument" + day.replace("-", "") + ".json"),
            TRADING_PARAMETERS.format(day=day.replace("-", "")),
            budget,
            fetch,
        )
        trading_doc = json.loads(trading_text)
        if trading_doc["report_date"] != day.replace("-", "") or any(
            r["TRADINGDAY"] != trading_doc["report_date"]
            for r in trading_doc["ContractDailyTradeArgument"]
        ):
            raise ResearchError("官方日内参数报告日期错配")
        trading = {
            r["INSTRUMENTID"]: r for r in trading_doc["ContractDailyTradeArgument"]
        }
        sources.extend([*references, receipt])
        for selected in [r for r in selections if r["date"] == day]:
            key, product = selected["contract"], selected["product"]
            base = data.metadata.get(key, day)
            opening, close = calendar.bounds(day, base)
            spec = None
            if product in specifications:
                try:
                    text, spec_receipt = specification_texts[product]
                    spec = contract_specifications(text, opening)
                    if any(base.get(f) != spec[f] for f in ("tick_size", "value_per_price")):
                        raise ResearchError("当时适用规范与训练报价步长不同，须单独重新校准")
                    spec["source"] = spec_receipt
                except ResearchError as exc:
                    spec_rejections[day + "/" + product] = str(exc)
            reasons = []
            if product not in calibration["products"]:
                reasons.append("training_insufficient")
            if products[product] not in {"SHFE", "INE"}:
                reasons.append("exchange_execution_not_reviewed")
            if spec is None:
                reasons.append("contract_specification_not_reviewed")
            row = previous.get(key)
            if not row:
                reasons.append("exact_previous_parameter_missing")
            else:
                reasons.extend(row["issues"])
                checks.append(timing_check(day, key, row, trading))
            if reasons:
                rejections[day + "/" + key] = reasons
                continue
            if available >= opening:
                raise ResearchError("前日官方报告未在本次开盘前发布")
            rules.append(
                {
                    "contract": key,
                    "trading_day": day,
                    "source_date": previous_day,
                    "available_at": available.isoformat(),
                    "margin_effective_at": at(previous_day, "15:00").isoformat(),
                    "effective_from": opening.isoformat(),
                    "effective_to": (close + MINUTE).isoformat(),
                    "tick_size": spec["tick_size"],
                    "value_per_price": spec["value_per_price"],
                    "specification_available_at": spec["available_at"],
                    "specification_effective_from": spec["effective_from"],
                    "specification_basis": spec["applicability_basis"],
                    "listed": row["listed"],
                    "expiry": row["last_trade_date"],
                    "margin_rate": row["exchange_speculation_margin"]["long"],
                    "fees": row["exchange_fee_reference"],
                    "sources": [*references, spec["source"]],
                }
            )
        print(
            json.dumps(
                {
                    "phase": "execution_time_review",
                    "date": day,
                    "rules_so_far": len(rules),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    write_csv(output / "margin_timing_checks.csv", checks, budget)
    write_json(
        output / "contract_specifications.json",
        {"qualified": specifications, "rejected": spec_rejections},
        budget,
    )
    # Do not pick a more favorable margin when independent historical rules disagree.
    if any(not r["matches"] for r in checks):
        raise ResearchError(
            "前日结算与当日日内保证金存在不一致，已保存对账；须复核后再运行诊断"
        )
    if not rules:
        raise ResearchError("没有取得执行资格的合约日，不能用空行情冒充诊断完成")
    qualification = {
        "schema": 1,
        "kind": "DIAGNOSTIC_EXECUTION_ONLY",
        "training_window": cfg["splits"]["train"],
        "validation_window": cfg["splits"]["validation"],
        "calibration_hash": digest(calibration),
        "fixed_ticks_hash": digest(cfg["strategy"]["fixed_ticks"]),
        "training_ready_products": sorted(calibration["products"]),
        "training_missing_products": calibration["missing_products"],
        "allowed_exchanges": ["SHFE", "INE"],
        "fee_model": "exchange_only",
        "rules": rules,
        "rule_rejections": rejections,
        "locked_test_read": False,
        "assumptions": [
            "研究假设，尚未确认：交易所公开一般持仓费率；账户加收手续费与保证金均为0，不代表实际账户成本。",
            "研究假设，尚未确认：手续费沿用上一完整交易日收盘后已发布的规则至下一日盘；没有据同日结算价格或未来费率改变盘中决策。",
            "研究假设，尚未确认：有日期的官方合约规范按日期当日末可获得并延续适用；逐交易日开盘检查，不要求早于训练截止。不代表完整历史规范审计；无日期当前页面仍保留历史适用证据缺口。",
            "研究假设，尚未确认：SHFE/INE日盘沿用官方交易时间页与本月节假日公告；其他交易所及完整历史合约池仍未核实。",
            "研究假设，尚未确认：缺少成交额，使用典型价成交量加权近似均价；分钟OHLC与下一可交易分钟开盘撮合，不能验证盘口排队。",
        ],
        "formal_blockers_retained": cfg.get("research_blockers", []),
        "ranking_policy": "原池、原R8排名、原Top-K；执行缺口只在排名后拒绝，不补选、不挪用分组资金",
        "margin_policy": "上一完整交易日收盘结算的一般持仓保证金；available_at来自官方update_date；日内表单独对账而不提前输入",
        "sources": list({r["path"]: r for r in sources}.values()),
    }
    cfg["execution"].update(
        qualification=qualification, qualification_hash=digest(qualification)
    )
    write_json(output / "execution_qualification.json", qualification, budget)
    write_json(output / "diagnostic_config.json", cfg, budget)
    arithmetic = fee_arithmetic_check(cfg, specifications, parameters, products, budget)
    write_json(output / "fee_arithmetic_checks.json", arithmetic, budget)
    write_json(
        output / "preparation_manifest.json",
        {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_configuration": str(Path(config).resolve()),
            "configuration_hash": digest(cfg),
            "training_fingerprint_verified": data.fingerprint,
            "qualification_hash": digest(qualification),
            "locked_test_read": False,
            "rule_count": len(rules),
            "qualified_specifications": sorted(specifications),
            "storage": budget.check(output),
            **code_identity(),
            "dependencies": dependencies(),
        },
        budget,
    )
    return {
        "config": str(output / "diagnostic_config.json"),
        "rules": len(rules),
        "fee_checks": len(arithmetic),
    }


def fee_arithmetic_check(cfg, specifications, directory, products, budget):
    """Independent real-official-fee arithmetic at declared example prices, not trades."""
    from .execution import fee

    # Chosen before inspecting performance: fixed, free-close-today, rate, multiplier=2.
    cases = [
        ("al2611", "al", Decimal("25000"), Decimal("25005")),
        ("au2612", "au", Decimal("1000"), Decimal("1001")),
        ("rb2701", "rb", Decimal("3000"), Decimal("3001")),
        ("cu2611", "cu", Decimal("100000"), Decimal("100010")),
    ]
    day = cfg["splits"]["train"]["end"]
    text, source = public_document(
        directory / ("Settlement" + day.replace("-", "") + ".json"),
        PARAMETERS["Settlement"].format(day=day.replace("-", "")),
        budget,
    )
    records = {r["INSTRUMENTID"]: r for r in json.loads(text)["Settlement"]}
    results = []
    for symbol, product, opening, closing in cases:
        if product not in specifications or symbol not in records:
            raise ResearchError("真实费用手算案例缺少合约规范或当日参数，不跳过")
        raw = records[symbol]
        base_rate, base_fixed, discount = (
            Decimal(raw[k]) for k in ("TRADEFEERATION", "TRADEFEEUNIT", "DISCOUNTRATE")
        )
        multiplier = Decimal(str(specifications[product]["value_per_price"]))
        # Explicit Decimal arithmetic independent of engine fee function.
        expected_open = opening * multiplier * base_rate + base_fixed
        expected_today = (closing * multiplier * base_rate + base_fixed) * discount
        expected_yesterday = closing * multiplier * base_rate + base_fixed
        meta = {
            "symbol": symbol,
            "value_per_price": float(multiplier),
            "fees": [{"effective_from": day, **fee_reference(raw)}],
        }
        actual = {
            offset: fee(meta, day, offset, float(price), 1)
            for offset, price in (
                ("open", opening),
                ("close_today", closing),
                ("close_yesterday", closing),
            )
        }
        expected = {
            "open": float(expected_open),
            "close_today": float(expected_today),
            "close_yesterday": float(expected_yesterday),
        }
        if any(abs(actual[k] - expected[k]) > 1e-9 for k in expected):
            raise ResearchError("真实官方费用与独立手算不一致")
        results.append(
            {
                "contract": symbol + "." + products[product],
                "reference_date": day,
                "example_open_price": float(opening),
                "example_close_price": float(closing),
                "quantity": 1,
                "example_prices_are_not_backtest_fills": True,
                "raw_rate": str(base_rate),
                "raw_fixed": str(base_fixed),
                "raw_close_today_multiplier": str(discount),
                "value_per_price": float(multiplier),
                "expected": expected,
                "engine": actual,
                "expected_round_trip_today": float(expected_open + expected_today),
                "passed": True,
                "sources": [source, specifications[product]["source"]],
            }
        )
    return results
