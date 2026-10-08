"""Official SHFE historical parameter references; never activate trading costs."""

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .acquisition import atomic_json
from .config import ResearchError, read_config
from .data import file_sha256
from .reporting import write_csv, write_json
from .storage import SpaceBudget

API_SOURCE = "https://www.shfe.com.cn/images/api.js"
PARAMETERS = {
    "ContractBaseInfo": "https://www.shfe.com.cn/data/busiparamdata/future/ContractBaseInfo{day}.dat",
    "Settlement": "https://www.shfe.com.cn/data/busiparamdata/future/Settlement{day}.dat",
}


def decimal_field(row, name):
    try:
        value = Decimal(str(row[name]))
    except (KeyError, InvalidOperation) as exc:
        raise ResearchError(f"官方参数缺失或非数值：{name}") from exc
    if not value.is_finite() or value < 0:
        raise ResearchError(f"官方参数非有限或负值：{name}")
    return value


def fee_reference(row):
    """Raw ratios are already fractions; the official page multiplies by 1000 for ‰."""
    rate = decimal_field(row, "TRADEFEERATION")
    fixed = decimal_field(row, "TRADEFEEUNIT")
    discount = decimal_field(row, "DISCOUNTRATE")
    if rate > 0 and fixed > 0:
        raise ResearchError("固定费+比例费复合收费，当前引擎尚不支持，不能遗漏一项")
    mode, value = ("rate", rate) if rate > 0 else ("fixed", fixed)
    return {
        "open": {"mode": mode, "value": float(value)},
        "close_today": {"mode": mode, "value": float(value * discount)},
        "close_yesterday": {"mode": mode, "value": float(value)},
    }


def parameter_rows(base, settlement, exchange_by_product):
    if base["report_date"] != settlement["report_date"]:
        raise ResearchError("官方合约参数和结算参数不是同一日期")
    day = datetime.strptime(base["report_date"], "%Y%m%d").date().isoformat()
    contracts = {r["INSTRUMENTID"]: r for r in base["ContractBaseInfo"]}
    if len(contracts) != len(base["ContractBaseInfo"]):
        raise ResearchError("官方合约参数中合约代码重复")
    seen, rows = set(), []
    for raw in settlement["Settlement"]:
        symbol = raw["INSTRUMENTID"]
        if symbol in seen:
            raise ResearchError("官方结算参数中合约代码重复")
        seen.add(symbol)
        meta = contracts.get(symbol)
        if not meta:
            raise ResearchError("结算参数合约不在同日官方合约目录中")
        if meta["EXCHANGEID"] != "SHFE" or not re.fullmatch(r"[a-zA-Z]+\d{4}", symbol):
            raise ResearchError("本适配器只解析上期所真实期货，不猜测其他交易所或期权")
        if any(r["TRADINGDAY"] != base["report_date"] for r in (raw, meta)):
            raise ResearchError("官方参数行日期与报告日期不一致")
        listing = datetime.strptime(meta["OPENDATE"], "%Y%m%d").date().isoformat()
        expiry = datetime.strptime(meta["EXPIREDATE"], "%Y%m%d").date().isoformat()
        problems, fees = [], None
        product = meta["COMMODITYID"]
        exchange = exchange_by_product.get(product)
        if exchange not in {"SHFE", "INE"}:
            problems.append("输入元数据没有该产品的SHFE/INE真实交易所映射，不猜测")
            exchange = None
        try:
            if product == "ec" and decimal_field(raw, "TRADEFEEUNIT") > 0:
                raise ResearchError(
                    "现金交割合约固定费用元/点口径须另行核实，不能当元/手"
                )
            fees = fee_reference(raw)
        except ResearchError as exc:
            problems.append(str(exc))
        margin = {}
        for direction, field in [
            ("long", "SPEC_LONGMARGINRATIO"),
            ("short", "SPEC_SHORTMARGINRATIO"),
        ]:
            try:
                value = float(decimal_field(raw, field))
                if not math.isfinite(value) or not 0 < value <= 1:
                    raise ResearchError(f"保证金比例无效：{field}")
                margin[direction] = value
            except ResearchError as exc:
                problems.append(str(exc))
        if len(margin) == 2 and margin["long"] != margin["short"]:
            problems.append("多空保证金不相同；当前引擎单一比例不能精确表示")
        rows.append(
            {
                "date": day,
                "contract": symbol + "." + exchange if exchange else None,
                "product_id": exchange + "." + product if exchange else None,
                "provider_instrument": symbol,
                "exchange": exchange,
                "reported_exchange_id": meta["EXCHANGEID"],
                "exchange_mapping_basis": "输入合约元数据的真实交易所代码；发布系统EXCHANGEID不可直接当上市交易所",
                "listed": listing,
                "last_trade_date": expiry,
                "exchange_fee_reference": fees,
                "exchange_speculation_margin": margin,
                "issues": problems,
                "raw_settlement_fields": raw,
                "reference_only": True,
                "broker_markup": None,
                "coverage_start": day,
                "coverage_end": day,
            }
        )
    if set(contracts) != seen:
        raise ResearchError("官方合约目录与结算参数覆盖不相等，禁止当作完整费用目录")
    return rows


def fetch_parameter(day, table, root, budget):
    """Serial, anonymous, bounded requests to the endpoint verified in official api.js."""
    import httpx

    date_code = day.replace("-", "")
    url = PARAMETERS[table].format(day=date_code)
    path = root / (table + date_code + ".json")
    receipt = path.with_suffix(".json.receipt.json")
    if path.exists():
        if not receipt.exists():
            raise ResearchError("既有官方参数缺少校验凭据，不覆盖")
        saved = json.loads(receipt.read_text())
        if saved["url"] != url or saved["sha256"] != file_sha256(path):
            raise ResearchError("官方参数来源/SHA256不一致，不覆盖")
    else:
        partial = path.with_suffix(".json.partial")
        budget.check(partial, reserve=1024 * 1024)
        size = 0
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(20, connect=8), follow_redirects=True
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as stream:
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > 2 * 1024**2:
                        raise ResearchError("官方参数响应超过2MiB上限")
                    budget.check(partial, reserve=len(chunk))
                    stream.write(chunk)
                    stream.flush()
        decoded = json.loads(partial.read_text())
        if decoded.get("report_date") != date_code or table not in decoded:
            raise ResearchError("接口实际报告日期/字段不符合请求，不使用其他日期补缺")
        partial.replace(path)
        atomic_json(
            receipt,
            {
                "url": url,
                "sha256": file_sha256(path),
                "bytes": size,
                "requested_utc": datetime.now(timezone.utc).isoformat(),
                "credential_used": False,
                "endpoint_documentation": API_SOURCE,
            },
            budget,
        )
    decoded = json.loads(path.read_text())
    if decoded.get("report_date") != date_code or table not in decoded:
        raise ResearchError("缓存的官方参数日期/字段不符合请求")
    return decoded, json.loads(receipt.read_text())


def review_costs(config, sources, output, fetch=False):
    from .experiments import code_identity

    cfg = read_config(config)
    exchange_by_product = {}
    for meta in cfg["metadata"]["contracts"]:
        product, exchange = meta["product"], meta["exchange"]
        if product in exchange_by_product and exchange_by_product[product] != exchange:
            raise ResearchError("产品代码对应多个交易所，需显式消除费用映射歧义")
        exchange_by_product[product] = exchange
    sources, output = Path(sources).resolve(), Path(output).resolve()
    policy = cfg.get("storage", {}).get("budget")
    if not policy:
        raise ResearchError("官方费用接入须设置包含原始、临时文件及结果的磁盘预算")
    budget = SpaceBudget(policy)
    budget.check(sources, reserve=4 * 1024**2)
    budget.check(output, reserve=8 * 1024**2)
    sources.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    earliest = min(w["start"] for w in cfg["splits"].values())
    latest = max(w["end"] for w in cfg["splits"].values())
    if earliest[:7] != latest[:7]:
        raise ResearchError("当前官方参数接入只允许一个自然月，不自动扩展多年")
    days = [d for d in cfg["calendar"]["trading_days"] if earliest <= d <= latest]
    preceding = [d for d in cfg["calendar"]["trading_days"] if d < earliest]
    if preceding:
        days = [preceding[-1], *days]
    rows, attempts, references = [], [], []
    for day in days:
        attempt = {"date": day, "status": "pending"}
        try:
            documents = {}
            for table in PARAMETERS:
                if (
                    not fetch
                    and not (
                        sources / (table + day.replace("-", "") + ".json")
                    ).exists()
                ):
                    raise ResearchError(
                        "没有该日已归档参数；需显式 --fetch-official 下载"
                    )
                documents[table], receipt = fetch_parameter(day, table, sources, budget)
                references.append(receipt)
            rows.extend(
                parameter_rows(
                    documents["ContractBaseInfo"],
                    documents["Settlement"],
                    exchange_by_product,
                )
            )
            attempt["status"] = "reference_complete"
        except Exception as exc:
            attempt.update(status="unavailable", reason=str(exc))
        attempts.append(attempt)
        # Persist each date before requesting the next. No forward-filled fee or margin.
        write_json(output / "progress.json", attempts, budget)
        print(json.dumps(attempt, ensure_ascii=False), flush=True)
    summary = {
        "status": "exchange_reference_only",
        "scope": sorted({r["exchange"] for r in rows if r["exchange"]}),
        "requested_days": days,
        "attempts": attempts,
        "references": references,
        "contract_days": len(rows),
        "ready_for_formal_backtest": False,
        "configuration_modified": False,
        "parameter_rows_with_issues": sum(bool(r["issues"]) for r in rows),
        "source_identity": code_identity(),
        "still_required": [
            "其他交易所同日历史合约目录、费用及保证金",
            "历史品种时段和完整日历",
            "费用口径须明确是否为交易所基础费，以及期货公司加收假设",
            "多空不对称保证金和复合收费需要扩展引擎；不能忽略",
            "本源不含合约跳动、价值和成交额换算系数，另需品种合约细则",
        ],
    }
    write_json(output / "exchange_reference.json", {**summary, "rows": rows}, budget)
    write_csv(output / "exchange_reference.csv.gz", rows, budget=budget)
    lines = [
        "# 上期所发布的SHFE/INE历史费用与合约资格参考",
        "",
        "只保存官方历史参考，不自动写入执行配置，不表示全市场或期货公司实际费用已核实。",
        "",
        f"实际取得 {len(rows)} 个合约日；报告有字段问题的 {summary['parameter_rows_with_issues']} 个。",
        "接口来自上期所公开api.js，逐日期核对report_date/TRADINGDAY，原始响应保留网址与SHA256；不跨日期填充。",
        "官方合约表中的EXCHANGEID对能源品种也返回SHFE。实际合同交易所按输入元数据映射为SHFE/INE，保留原字段；不将sc、bc、ec、lu、nr误记为SHFE。",
        "TRADEFEERATION原值是比例，不是千分数；官方网页乘1000展示‰。TRADEFEEUNIT是元/手。",
        "平今参考费=基础交易费×DISCOUNTRATE，0表示该项减免，2表示基础项的两倍。",
        "只使用一般持仓字段SPEC_*，不以套保优惠替代普通研究仓位。复合收费及多空保证金不一致时显式列问题。",
        "OPENDATE是官方上市日期、EXPIREDATE是最后交易日；不以交割结束日替代。",
        "这些报告含收市结算价，仅归档为费用参考；收市价不进入排名、信号、成交或训练跳数统计。",
        "",
        "| 日期 | 状态 | 原因 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {r['date']} | {r['status']} | {r.get('reason', '')} |" for r in attempts
    )
    lines += ["", *["- " + r for r in summary["still_required"]], ""]
    body = "\n".join(lines)
    budget.check(output / "report.md", reserve=len(body.encode()))
    (output / "report.md").write_text(body)
    return {
        "report": str(output / "report.md"),
        "contract_days": len(rows),
        "unavailable_days": sum(r["status"] == "unavailable" for r in attempts),
    }
