"""Product-day reconciliation and execution readiness, without return evaluation."""

import copy
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .acquisition import atomic_json, month_bounds
from .calendar import MINUTE
from .config import ResearchError, digest, read_config
from .data import file_sha256, load_data
from .reporting import write_csv, write_json
from .signals import rank_candidates
from .storage import SpaceBudget


def product_id(meta):
    return meta["exchange"] + "." + meta["product"]


def split_days(cfg):
    return {
        name: [
            d for d in cfg["calendar"]["trading_days"] if w["start"] <= d <= w["end"]
        ]
        for name, w in cfg["splits"].items()
    }


def lock_splits(cfg, path, budget=None):
    """Freeze date boundaries; this does not authorize opening the final test set."""
    path = Path(path)
    specification = {
        "schema": 1,
        "kind": "research_split_only",
        "splits": copy.deepcopy(cfg["splits"]),
        "trading_days": split_days(cfg),
    }
    if path.exists():
        existing = json.loads(path.read_text())
        if any(existing.get(k) != v for k, v in specification.items()):
            raise ResearchError("既有时间划分锁不一致，不覆盖；须另立研究版本")
        return existing
    body = {
        **specification,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "label": "沿用已配置时间划分；不是冻结策略或最佳参数",
        "configuration_hash_before_lock": digest(cfg),
    }
    if budget:
        budget.check(path, reserve=8192)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, indent=2)
    return body


def reconcile_products(data, days, catalogue):
    """One mutually exclusive stage per catalog product and trading day."""
    universe = sorted(catalogue["products"])
    meta_products = {
        key: product_id(records[0]) for key, records in data.metadata.records.items()
    }
    contracts = defaultdict(list)
    for key, product in meta_products.items():
        contracts[product].append(key)
    rows, totals, selected_records, errors = [], [], [], []
    for day in days:
        pool, rejected = data.pool(day)
        # The same eight-minute cutoffs used by execution, including calendar overrides.
        cutoff = max(
            (data.calendar.bounds(day, r["meta"])[0] + 8 * MINUTE for r in pool),
            default=None,
        )
        ranked, rank_rejected = (
            rank_candidates(data, day, pool, cutoff, data.cfg["strategy"]["k"])
            if cutoff
            else ([], [])
        )
        by_product = {product_id(r["meta"]): r for r in pool}
        by_key = {r["contract"]: r for r in ranked}
        rank_errors = {r["contract"]: r for r in rank_rejected}
        rejected_products = defaultdict(Counter)
        for row in rejected:
            if product := meta_products.get(row["contract"]):
                rejected_products[product][row["reason"]] += 1
        for product in universe:
            group = "financial" if product.startswith("CFFEX.") else "commodity"
            result = {
                "date": day,
                "product_id": product,
                "group": group,
                "previous_day": data.calendar.previous(day),
                "contract": None,
                "stage": None,
                "primary_reason": None,
                "reasons": [],
                "contract_rejection_counts": dict(
                    sorted(rejected_products[product].items())
                ),
            }
            if product not in by_product:
                reasons = sorted(rejected_products[product])
                # Lower-OI competitors never represent a product-level exclusion.
                reasons = [r for r in reasons if r != "lower_previous_close_oi"]
                order = [
                    "previous_daily_observation_missing_or_incomplete",
                    "previous_trading_day_incomplete",
                    "missing_previous_night_calendar",
                    "previous_close_oi_missing",
                    "no_previous_eligible_contract",
                    "not_listed_expired_or_metadata_missing",
                ]
                primary = next(
                    (r for r in order if r in reasons), "no_catalog_contract_metadata"
                )
                if primary == "previous_close_oi_missing":
                    # Preserve the engine's reason and distinguish known zero from missing OI.
                    previous = data.calendar.previous(day)
                    observations = [
                        data.daily_by_day.get((previous, key))
                        for key in contracts[product]
                        if data.metadata.eligible(data.metadata.get(key, day), day)
                        and data.metadata.eligible(
                            data.metadata.get(key, previous or ""), previous or ""
                        )
                    ]
                    if observations and all(
                        r and r.complete and r.open_interest == 0 for r in observations
                    ):
                        primary = "no_positive_previous_oi"
                result.update(
                    stage="pool_excluded", primary_reason=primary, reasons=reasons
                )
            else:
                chosen = by_product[product]
                key = chosen["contract"]
                result.update(
                    contract=key,
                    previous_oi=chosen["previous_oi"],
                    previous_volume=chosen["previous_volume"],
                    selection_day=chosen["selection_day"],
                )
                selected_records.append(
                    {k: v for k, v in chosen.items() if k != "meta"}
                )
                if key in by_key:
                    row = by_key[key]
                    result.update(
                        stage="directional_ranked",
                        direction=row["direction"],
                        r8=row["r8"],
                        rank=row["rank"],
                        selected_for_k=row["selected"],
                        ranking_time=row["ranking_time"],
                    )
                elif key in rank_errors:
                    reasons = rank_errors[key]["reasons"]
                    result.update(
                        stage="ranking_excluded",
                        primary_reason=reasons[0],
                        reasons=reasons,
                    )
                else:
                    result.update(
                        stage="unaccounted", primary_reason="ranking_result_missing"
                    )
                    errors.append(
                        {
                            "date": day,
                            "product_id": product,
                            "reason": "ranking_result_missing",
                        }
                    )
            rows.append(result)
        outside = set(by_product) - set(universe)
        if outside:
            errors.append(
                {
                    "date": day,
                    "reason": "pool_outside_catalogue",
                    "products": sorted(outside),
                }
            )
        for group in sorted({r["group"] for r in rows if r["date"] == day}):
            subset = [r for r in rows if r["date"] == day and r["group"] == group]
            counts = Counter(r["stage"] for r in subset)
            totals.append(
                {
                    "date": day,
                    "group": group,
                    "initial": len(subset),
                    "pool_excluded": counts["pool_excluded"],
                    "pool_selected": counts["ranking_excluded"]
                    + counts["directional_ranked"],
                    "ranking_excluded": counts["ranking_excluded"],
                    "directional_ranked": counts["directional_ranked"],
                    "primary_exclusions": dict(
                        Counter(
                            r["primary_reason"] for r in subset if r["primary_reason"]
                        )
                    ),
                }
            )
    summary = {
        "catalogue_products": len(universe),
        "research_days": len(days),
        "initial_product_days": len(rows),
        "pool_excluded_product_days": sum(r["stage"] == "pool_excluded" for r in rows),
        "selected_product_days": len(selected_records),
        "ranking_excluded_product_days": sum(
            r["stage"] == "ranking_excluded" for r in rows
        ),
        "directional_ranked_product_days": sum(
            r["stage"] == "directional_ranked" for r in rows
        ),
        "minute_covered_products": sorted({meta_products[b.key] for b in data.bars}),
        "catalogue_without_minutes": sorted(
            set(universe) - {meta_products[b.key] for b in data.bars}
        ),
        "index_without_real_contract_catalogue": sorted(
            set(catalogue.get("indexed_products", [])) - set(universe)
        ),
        "historical_universe_verified": catalogue.get(
            "historical_universe_verified", False
        ),
        "primary_exclusion_counts": dict(
            Counter(r["primary_reason"] for r in rows if r["primary_reason"])
        ),
        "equations_hold": all(r["stage"] != "unaccounted" for r in rows) and not errors,
        "errors": errors,
    }
    return summary, rows, totals, selected_records


def volume_audit(data, plan):
    purpose = {(r["trading_day"], r["contract"]): r["purpose"] for r in plan}
    if len(purpose) != len(plan):
        raise ResearchError("分钟计划中合约日重复，不能唯一归因研究/预热")
    buckets = {}
    missing = set()
    for bar in data.bars:
        identity = bar.trading_day, bar.key
        if identity not in purpose:
            missing.add(identity)
        kind = purpose.get(identity, "unplanned")
        split = next(
            (
                name
                for name, w in data.cfg["splits"].items()
                if w["start"] <= bar.trading_day <= w["end"]
            ),
            "pre_month",
        )
        key = identity + (kind, split)
        item = buckets.setdefault(
            key,
            {
                "date": bar.trading_day,
                "contract": bar.key,
                "product_id": bar.exchange + "." + bar.product,
                "purpose": kind,
                "calendar_split": split,
                "minutes": 0,
                "zero_volume_minutes": 0,
                "volume_lots": 0,
                "nonflat_zero_volume_minutes": 0,
            },
        )
        item["minutes"] += 1
        item["volume_lots"] += bar.volume
        item["zero_volume_minutes"] += bar.volume == 0
        item["nonflat_zero_volume_minutes"] += bar.volume == 0 and (
            bar.high != bar.low or bar.open != bar.close
        )
    contract_days = []
    aggregate = {}
    for _, row in sorted(buckets.items()):
        row["zero_volume_ratio"] = row["zero_volume_minutes"] / row["minutes"]
        row["whole_day_zero_volume"] = row["volume_lots"] == 0
        contract_days.append(row)
        key = row["product_id"], row["purpose"], row["calendar_split"]
        item = aggregate.setdefault(
            key,
            {
                "product_id": key[0],
                "purpose": key[1],
                "calendar_split": key[2],
                "minutes": 0,
                "zero_volume_minutes": 0,
                "volume_lots": 0,
                "contract_days": 0,
                "whole_day_zero_volume_days": 0,
            },
        )
        for field in ("minutes", "zero_volume_minutes", "volume_lots"):
            item[field] += row[field]
        item["contract_days"] += 1
        item["whole_day_zero_volume_days"] += row["whole_day_zero_volume"]
    products = list(aggregate.values())
    for row in products:
        row["zero_volume_ratio"] = row["zero_volume_minutes"] / row["minutes"]
    summary = {}
    for kind in sorted({r["purpose"] for r in contract_days}):
        subset = [r for r in contract_days if r["purpose"] == kind]
        summary[kind] = {
            "minutes": sum(r["minutes"] for r in subset),
            "zero_volume_minutes": sum(r["zero_volume_minutes"] for r in subset),
            "whole_day_zero_volume_days": sum(
                r["whole_day_zero_volume"] for r in subset
            ),
        }
        summary[kind]["zero_volume_ratio"] = (
            summary[kind]["zero_volume_minutes"] / summary[kind]["minutes"]
        )
    return (
        {"by_purpose": summary, "unplanned_contract_days": sorted(missing)},
        products,
        contract_days,
    )


def execution_checklist(data, product_days):
    """A review template for actual selected contracts; never supplies made-up costs."""
    selected = defaultdict(list)
    for row in product_days:
        if row["contract"]:
            selected[row["contract"]].append(row["date"])
    items = []
    for key, days in sorted(selected.items()):
        versions = {}
        for day in days:
            meta = data.metadata.get(key, day)
            versions[meta["effective_from"]] = meta
        items.append(
            {
                "contract": key,
                "product_id": product_id(next(iter(versions.values()))),
                "selected_dates": sorted(days),
                "metadata_versions": list(versions.values()),
                "required_review": [
                    "历史上市及交易资格",
                    "历史最小跳动及合约价值",
                    "历史交易时段",
                    "开仓/平今/平昨费用及生效区间",
                    "保证金及生效区间",
                    "近似均价模式保留；精确VWAP另需成交额及换算系数",
                ],
                "broker_markup": None,
                "review_status": "unverified"
                if any(not m.get("verified") for m in versions.values())
                else "verified",
            }
        )
    return items


def audit_month(settings, output):
    from .experiments import code_identity, dependencies

    root, output = Path(settings["root"]), Path(output).resolve()
    cfg = read_config(root / "config.json")
    budget = SpaceBudget(settings["budget"])
    budget.check(output, reserve=8 * 1024**2)
    output.mkdir(parents=True, exist_ok=True)
    catalogue = json.loads((root / "catalogue_audit.json").read_text())
    plan = json.loads((root / "minute_plan.json").read_text())
    data = load_data(
        cfg
    )  # All splits are inspected for quality only, never returns or calibration.
    start, end = month_bounds(settings["month"])
    days = [d for d in data.calendar.days if start.isoformat() <= d < end.isoformat()]
    summary, rows, totals, selected = reconcile_products(data, days, catalogue)
    volume, product_volume, contract_volume = volume_audit(data, plan)
    archived = json.loads((root / "daily_selection.json").read_text())
    if sorted(archived, key=lambda r: (r["date"], r["contract"])) != sorted(
        selected, key=lambda r: (r["date"], r["contract"])
    ):
        summary["errors"].append(
            {"reason": "archived_pool_differs_from_recomputed_pool"}
        )
    if volume["unplanned_contract_days"]:
        summary["errors"].append({"reason": "unplanned_minute_data"})
    summary["equations_hold"] = summary["equations_hold"] and not summary["errors"]
    lock = lock_splits(cfg, output / "split_lock.json", budget)
    locked_config = copy.deepcopy(cfg)
    locked_config["split_lock"] = lock
    locked_config["config_path"] = str(output / "locked_config.json")
    locked_path = output / "locked_config.json"
    if locked_path.exists() and json.loads(locked_path.read_text()) != locked_config:
        raise ResearchError("既有锁定配置已修改或来源配置不同，不覆盖；请另用输出版本")
    if not locked_path.exists():
        atomic_json(locked_path, locked_config, budget)
    snapshot = {
        "month": settings["month"],
        "status": "data_audit_only",
        "strategy_returns_evaluated": False,
        "locked_test_quality_audit_only": True,
        "reconciliation": summary,
        "zero_volume": volume,
        "split_lock": lock,
        "split_counts": {k: len(v) for k, v in lock["trading_days"].items()},
        "data_fingerprint": data.fingerprint,
        "configuration_hash": digest(cfg),
        "inputs": {
            name: file_sha256(root / name)
            for name in [
                "config.json",
                "metadata.json",
                "calendar.json",
                "catalogue_audit.json",
                "minute_plan.json",
                "daily_selection.json",
            ]
        },
        "formal_configuration_gaps": data.quality["configuration_gaps"],
        "dependencies": dependencies(),
        **code_identity(),
    }
    write_json(output / "readiness.json", snapshot, budget)
    write_json(output / "product_days.json", rows, budget)
    write_csv(output / "product_days.csv", rows, budget=budget)
    write_csv(output / "daily_group_counts.csv", totals, budget=budget)
    write_csv(output / "zero_volume_products.csv", product_volume, budget=budget)
    write_csv(output / "zero_volume_contract_days.csv", contract_volume, budget=budget)
    write_json(
        output / "execution_checklist.json", execution_checklist(data, rows), budget
    )
    missing_covered = [
        r
        for r in rows
        if r["product_id"] in summary["minute_covered_products"]
        and r["stage"] == "pool_excluded"
    ]
    write_csv(
        output / "covered_product_day_exclusions.csv", missing_covered, budget=budget
    )
    lines = [
        f"# {settings['month']} 品种日对账与执行准备复核",
        "",
        "本报告只核对数据、排名与配置，不提供策略收益、最佳K或测试集选参结论。",
        "",
        f"目录 {summary['catalogue_products']} 品种 × {len(days)} 交易日 = {len(rows)} 品种日。",
        f"{len(rows)} = {summary['pool_excluded_product_days']} 个池前排除 + {summary['selected_product_days']} 个已选真实合约日。",
        f"{summary['selected_product_days']} = {summary['ranking_excluded_product_days']} 个开盘排名排除 + {summary['directional_ranked_product_days']} 个有效方向排名。",
        f"守恒与重算一致：{summary['equations_hold']}；差异：{summary['errors']}。",
        "",
        "未覆盖目录品种：" + ", ".join(summary["catalogue_without_minutes"]) + "。",
        "指数目录有、真实合约目录无："
        + ", ".join(summary["index_without_real_contract_catalogue"])
        + "。",
        f"已覆盖品种仍被池前排除的品种日 {len(missing_covered)} 个，逐项见 covered_product_day_exclusions.csv。",
        "池前排除保留每个交割合同的全部原因，主因仅用于互斥对账；后置策略过滤不参与排名池。",
        "",
        "## 零成交量",
        "",
        "| 用途 | 分钟数 | 零量分钟 | 比例 | 整日零量合约日 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, row in volume["by_purpose"].items():
        lines.append(
            f"| {name} | {row['minutes']} | {row['zero_volume_minutes']} | {row['zero_volume_ratio']:.2%} | {row['whole_day_zero_volume_days']} |"
        )
    lines += [
        "",
        "用途严格取分钟任务清单。月内换月合约的历史预热仍计 warmup，不能按自然月把它们当作研究成交数据。",
        "分品种、合约、日期、用途及日历切分见 zero_volume*.csv。零量记录保留、不可成交；volume>0只是分钟成交代理，不能证明盘口或订单容量。",
        "",
        "## 已锁定时间划分",
        "",
        "| 区间 | 开始 | 结束（含） | 交易日数 |",
        "| --- | --- | --- | ---: |",
    ]
    for name, w in cfg["splits"].items():
        lines.append(
            f"| {name} | {w['start']} | {w['end']} | {len(lock['trading_days'][name])} |"
        )
    lines += [
        "",
        "边界沿用获取数据前已有的 acquire.json/config.json。可见的先前指令未载明60/20/20硬约束，不把网页推测当作已批准的划分要求。",
        "split_lock.json只锁时间。locked_config.json供校准和基准命令使用，修改边界或对应交易日会被拒绝；最终测试仍必须经过验证后freeze。",
        "本次检查测试段的数据质量，不读取测试段收益或波动来校准跳数。",
        "",
        "## 尚待核实",
        "",
        "缺失0只指配置时段和所选合约；供应商最新目录不是完整历史合约池，首个日线日期不是已核实上市日期。",
        "当前仍是典型价量权近似均价；没有成交额。交易所收费文件不自动等于期货公司实际费率，历史生效区间、平今区别和保证金调整须逐项记录。",
        f"现有广义配置检查报告 {len(data.quality['configuration_gaps'])} 个合同/字段缺口，详见 readiness.json；实际已选合同复核清单见 execution_checklist.json。",
        "本报告未下载额外月份、未修改策略条件、未连接账户、未发送委托，也没有运行收益Top-K搜索。",
        "",
    ]
    body = "\n".join(lines)
    budget.check(output / "report.md", reserve=len(body.encode()))
    (output / "report.md").write_text(body, encoding="utf-8")
    return {
        "report": str(output / "report.md"),
        "reconciliation_errors": len(summary["errors"]),
        "initial_product_days": len(rows),
        "selected_product_days": len(selected),
        "directional_ranked_product_days": summary["directional_ranked_product_days"],
        "covered_product_day_exclusions": len(missing_covered),
        "split_counts": snapshot["split_counts"],
    }
