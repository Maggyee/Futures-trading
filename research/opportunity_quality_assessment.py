"""Describe fixed opportunity labels without selecting a trading strategy."""

import json
import tarfile
from collections import Counter, defaultdict

import numpy as np

from .calendar import MINUTE, stamp
from .coverage_expansion import ROOT
from .data import file_sha256
from .opportunity_quality import (
    FILTERS,
    FREEZE,
    PLAN,
    merge_segments,
    read_records,
    require,
    verify_freeze,
)
from .reporting import write_csv, write_json


def daily_values(rows, field, horizon):
    units = defaultdict(list)
    for row in rows:
        value = row.get("labels", {}).get(str(horizon), {}).get(field)
        if value is not None:
            units[(row["date"], row["contract"], row["direction"])].append(value)
    days = defaultdict(list)
    for (day, _, _), values in units.items():
        days[day].append(float(np.mean(values)))
    return {day: float(np.mean(values)) for day, values in sorted(days.items())}


def estimate(values):
    data = np.array(list(values), dtype=float)
    if not len(data):
        return {"mean": None, "days": 0, "day_bootstrap_95": None}
    interval = None
    if len(data) >= 2:
        rng = np.random.default_rng(20261009)
        means = rng.choice(data, size=(2000, len(data)), replace=True).mean(axis=1)
        interval = [float(v) for v in np.quantile(means, [.025, .975])]
    return {"mean": float(data.mean()), "days": len(data), "day_bootstrap_95": interval}


def summarize(rows, horizon=15):
    metrics = ("raw_atr", "raw_bps", "mfe_atr", "mae_atr", "mfe_less_signal_cost_atr",
               "net_atr", "net_bps", "extra_tick_net_atr")
    result = {"rows": len(rows), "contract_day_directions": len({(r["date"], r["contract"], r["direction"]) for r in rows}),
              "trading_days": len({r["date"] for r in rows}), "products": dict(Counter(r["product"] for r in rows))}
    result["metrics"] = {field: estimate(daily_values(rows, field, horizon).values()) for field in metrics}
    raw = [r for r in rows if r.get("labels", {}).get(str(horizon), {}).get("raw_status") == "complete"]
    economic = [r for r in raw if r["labels"][str(horizon)]["economic_status"] == "complete"]
    result.update(raw_complete=len(raw), economic_complete=len(economic),
                  positive_net_rows=sum(r["labels"][str(horizon)]["net_atr"] > 0 for r in economic),
                  cost_pass_rows=sum(r["cost_pass"] is True for r in rows),
                  account_signal_feasible_rows=sum((r["quantity_signal_empty"] or 0) > 0 for r in rows),
                  quantity_and_guard_feasible_rows=sum(r["labels"][str(horizon)].get("quantity_and_guard_feasible", False) for r in economic),
                  censor_reasons=dict(Counter(r.get("labels", {}).get(str(horizon), {}).get("reason", "not_requested") for r in rows
                                              if r.get("labels", {}).get(str(horizon), {}).get("economic_status") != "complete")))
    result["raw_atr_same_economic_subset"] = estimate(daily_values(economic, "raw_atr", horizon).values())
    product_means = [float(np.mean(list(daily_values([r for r in economic if r["product"] == product], "net_atr", horizon).values())))
                     for product in sorted({r["product"] for r in economic})]
    result["product_equal_net_atr"] = {"mean": float(np.mean(product_means)) if product_means else None,
                                       "products": len(product_means), "basis": "equal product mean; descriptive sensitivity"}
    return result


def pair_summary(pairs, by_id, horizon):
    usable, unit_differences, days = [], defaultdict(list), defaultdict(list)
    raw_differences, raw_days = defaultdict(list), defaultdict(list)
    for pair in pairs:
        treatment, control = by_id[pair["treatment"]], by_id[pair["control"]]
        a, b = treatment["labels"][str(horizon)], control["labels"][str(horizon)]
        identity = treatment["date"], treatment["contract"], treatment["direction"]
        if a["raw_status"] == b["raw_status"] == "complete":
            raw_differences[identity].append(a["raw_atr"] - b["raw_atr"])
        if a["economic_status"] == b["economic_status"] == "complete":
            usable.append(pair)
            unit_differences[identity].append(a["net_atr"] - b["net_atr"])
    for (day, _, _), values in unit_differences.items():
        days[day].append(float(np.mean(values)))
    for (day, _, _), values in raw_differences.items():
        raw_days[day].append(float(np.mean(values)))
    return {"matched_pairs": len(pairs), "economic_complete_pairs": len(usable),
            "unique_treatment_segments": len({by_id[p["treatment"]]["segment_id"] for p in pairs}),
            "unique_control_segments": len({by_id[p["control"]]["segment_id"] for p in pairs}),
            "net_atr_difference": estimate(float(np.mean(v)) for v in days.values()),
            "raw_atr_difference": estimate(float(np.mean(v)) for v in raw_days.values()),
            "mean_liquidity_log_difference": float(np.mean([p["liquidity_log_difference"] for p in pairs])) if pairs else None,
            "mean_relative_atr_log_difference": float(np.mean([p["relative_atr_log_difference"] for p in pairs])) if pairs else None}


def assessments(observations, shapes, pairs, legacy):
    representatives = [r for r in observations if r["representative"]]
    by_id = {r["id"]: r for r in observations}
    require(len(by_id) == len(observations), "观察ID重复")
    ranking, matching, filters, shape_groups, legacy_groups, sensitivities = {}, {}, {}, {}, {}, {}
    for horizon in (5, 15, 30):
        ranking[str(horizon)] = {group: summarize([r for r in representatives if r["rank_group"] == group], horizon)
                                for group in ("rank_1_2", "rank_3_5", "rank_6_plus")}
        matching[str(horizon)] = {}
        for comparison in ("rank_1_2_vs_rank_3_5", "rank_1_2_vs_rank_6_plus", "rank_3_5_vs_rank_6_plus"):
            matched = pair_summary([p for p in pairs if p["comparison"] == comparison], by_id, horizon)
            targets = [r for r in representatives if r["rank_group"] == comparison.split("_vs_")[0]]
            matched["eligible_target_segments"] = len(targets)
            matched["target_match_fraction"] = matched["unique_treatment_segments"] / len(targets) if targets else None
            matching[str(horizon)][comparison] = matched
    for condition in FILTERS:
        retained = [r for r in representatives if r["conditions"][condition]]
        rejected = [r for r in representatives if not r["conditions"][condition]]
        filters[condition] = {"retained": summarize(retained), "rejected": summarize(rejected),
                              "retained_by_horizon": {str(h): summarize(retained, h) for h in (5, 30)},
                              "rejected_by_horizon": {str(h): summarize(rejected, h) for h in (5, 30)}}
    sensitivity_horizons = {}
    for name, subset in (
        ("ma10_recovered", [r for r in shapes if r["reference"] == "10"]),
        ("ma20_recovered", [r for r in shapes if r["reference"] == "20" and r["shape_action"] == "recovered"]),
        ("ma20_recovered_below_ma10", [r for r in shapes if r["reference"] == "20" and r["shape_action"] == "recovered" and not r["reclaimed_ma10"]]),
        ("ma20_reclaimed_ma10", [r for r in shapes if r["shape_action"] == "reclaimed_ma10"]),
        ("dual_recovered", [r for r in shapes if r["reference"] == "dual"]),
    ):
        merged = merge_segments([dict(r) for r in subset])
        first = [r for r in merged if r["representative"]]
        shape_groups[name] = {"raw_version_rows": len(subset), "touches": len({(r["date"], r["contract"], r["touch_id"]) for r in subset}),
                              "segments": summarize(first), "by_horizon": {str(h): summarize(first, h) for h in (5, 30)}}
    for name, subset in (
        ("control_before_cutoff", [r for r in legacy if r["source"] == "legacy_control" and r["before_cutoff"]]),
        ("control_cost_rejected", [r for r in legacy if r["source"] == "legacy_control" and r["before_cutoff"] and r["cost_pass"] is False]),
        ("control_account_rejected", [r for r in legacy if r["source"] == "legacy_control" and r["before_cutoff"] and r["cost_pass"] is True and r["quantity_signal_empty"] == 0]),
        ("lifetime_formed", [r for r in legacy if r["source"] == "legacy_lifetime"]),
    ):
        merged = merge_segments([dict(r) for r in subset])
        first = [r for r in merged if r["representative"]]
        legacy_groups[name] = {"raw_observations": len(subset), "segments": summarize(first),
                               "by_horizon": {str(h): summarize(first, h) for h in (5, 30)}}
    for name, subset in (
        ("non_lc", [r for r in representatives if r["product"].lower() != "lc"]),
        ("commodity", [r for r in representatives if r["group"] == "commodity"]),
        ("financial", [r for r in representatives if r["group"] == "financial"]),
        ("cost_rejected", [r for r in representatives if r["cost_pass"] is False]),
        ("cost_passed", [r for r in representatives if r["cost_pass"] is True]),
        ("cost_pass_account_rejected", [r for r in representatives if r["cost_pass"] is True and r["quantity_signal_empty"] == 0]),
    ):
        sensitivities[name] = summarize(subset)
        sensitivity_horizons[name] = {str(h): summarize(subset, h) for h in (5, 30)}
    overlap = {}
    for i, a in enumerate(FILTERS):
        for b in FILTERS[i + 1:]:
            together = sum(r["conditions"][a] and r["conditions"][b] for r in representatives)
            either = sum(r["conditions"][a] or r["conditions"][b] for r in representatives)
            overlap[a + "/" + b] = {"both": together, "either": either, "jaccard": together / either if either else None}
    touch_versions = defaultdict(dict)
    for row in shapes:
        if row["reference"] == "20":
            touch_versions[(row["date"], row["contract"], row["touch_id"])][row["shape_action"]] = row
    timing_pairs = []
    for identity, versions in sorted(touch_versions.items()):
        if "recovered" not in versions or "reclaimed_ma10" not in versions:
            continue
        a, b = versions["recovered"], versions["reclaimed_ma10"]
        x, y = a["labels"]["15"], b["labels"]["15"]
        timing_pairs.append({"date": identity[0], "contract": identity[1], "touch_id": identity[2],
                             "recovery_time": a["time"], "reclaim_time": b["time"],
                             "extra_wait_trading_minutes": (stamp(b["time"]) - stamp(a["time"])) / MINUTE,
                             "recovery_below_ma10": not a["reclaimed_ma10"],
                             "recovery_net_bps": x.get("net_bps"), "reclaim_net_bps": y.get("net_bps"),
                             "recovery_quantity_feasible": x.get("quantity_and_guard_feasible"),
                             "reclaim_quantity_feasible": y.get("quantity_and_guard_feasible")})
    return {"base": summarize(representatives), "ranking": ranking, "matching": matching,
            "filters": filters, "filter_overlap": overlap, "shapes": shape_groups,
            "legacy": legacy_groups, "sensitivities": sensitivities, "sensitivities_by_horizon": sensitivity_horizons,
            "ma20_timing_pairs": timing_pairs,
            "products": {p: summarize([r for r in representatives if r["product"] == p]) for p in sorted({r["product"] for r in representatives})}}


def fmt(value):
    return "未知" if value is None else f"{value:.3f}"


def render(assessment):
    lines = ["# 开盘排名与入场形态的机会质量研究", "",
             "固定15个交易分钟为主要口径，5/30分钟为对照；只分析已查看的52个开发交易日。没有新策略矩阵、真实账户回报或样本外证明。", "",
             "下表以每个合约／日／方向先平均、每个交易日再平均；每个日期等权。数值是价格标签／观察时已知1分钟ATR，不是收益率。", "",
             "| 窗口 | 排名 | 机会段 | 日期 | 完整经济标签 | 毛位移/ATR | 扣费位移/ATR | 有利运动/ATR | 不利运动/ATR |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for month, item in assessment["windows"].items():
        for group, row in item["ranking"]["15"].items():
            values = [row["metrics"][name]["mean"] for name in ("raw_atr", "net_atr", "mfe_atr", "mae_atr")]
            lines.append(f"| {month} | {group} | {row['rows']} | {row['trading_days']} | {row['economic_complete']} | " + " | ".join(fmt(v) for v in values) + " |")
    lines += ["", "毛位移可能包括缺乏可核实费用的品种；扣费标签只包含执行资料齐备的子集，完整人数须一起看。assessment同时列出相同经济子集的毛位移。", "",
              "| 窗口 | 同时刻匹配 | 匹配对 | 双边完整经济标签 | 主口径净位移差/ATR | 日期成组95%区间 |", "|---|---|---:|---:|---:|---|"]
    for month, item in assessment["windows"].items():
        for comparison, row in item["matching"]["15"].items():
            estimate = row["net_atr_difference"]
            lines.append(f"| {month} | {comparison} | {row['matched_pairs']} | {row['economic_complete_pairs']} | {fmt(estimate['mean'])} | {estimate['day_bootstrap_95']} |")
    lines += ["", "匹配仅使用当时已知的成交量与相对ATR；未根据未来标签挑对照。相同产品在同日只有一个入池合约，无法消除品种差异；匹配可重复使用同一控制机会段的不同钟点，统计按日期成组，不视为额外独立样本。", "",
              "| 窗口 | 单独条件 | 保留段 | 排除段 | 保留净位移/ATR | 排除净位移/ATR | 排除的正净标签 |", "|---|---|---:|---:|---:|---:|---:|"]
    for month, item in assessment["windows"].items():
        for condition, rows in item["filters"].items():
            keep, reject = rows["retained"], rows["rejected"]
            lines.append(f"| {month} | {condition} | {keep['rows']} | {reject['rows']} | {fmt(keep['metrics']['net_atr']['mean'])} | {fmt(reject['metrics']['net_atr']['mean'])} | {reject['positive_net_rows']} |")
    lines += ["", "各条件单独作用于同一基础机会段的第一个观察钟点；不重新选择稍后才通过条件的观察。正净标签是固定期限研究结果，不是可以按未来结果挑出的交易。", "",
              "| 窗口 | 回踩确认定义 | 原事件版本 | 触碰 | 归并段 | 净位移/ATR |", "|---|---|---:|---:|---:|---:|"]
    for month, item in assessment["windows"].items():
        for name, rows in item["shapes"].items():
            segment = rows["segments"]
            lines.append(f"| {month} | {name} | {rows['raw_version_rows']} | {rows['touches']} | {segment['rows']} | {fmt(segment['metrics']['net_atr']['mean'])} |")
    lines += ["", "MA20先恢复与再站回MA10是同一次触碰的两种观察，不能相加。双触碰单列。生命周期记录等待、失效与过期；本轮未替换可交易回踩规则。", "",
              "| 窗口 | 原发布案例 | 原始观察 | 归并段 | 15分钟净位移/ATR |", "|---|---|---:|---:|---:|"]
    for month, item in assessment["windows"].items():
        for name, rows in item["legacy"].items():
            segment = rows["segments"]
            lines.append(f"| {month} | {name} | {rows['raw_observations']} | {segment['rows']} | {fmt(segment['metrics']['net_atr']['mean'])} |")
    lines += ["", "经济标签使用下一日内交易分钟开盘及固定第h分钟收盘，含原不利滑点与可核实交易所费用。缺失分钟不会跳过；未知费用不填零；价格路径可完整而经济标签未知。MFE不可当作能够成交的盈利。", "",
              "整数容量只表示100万元空仓账户的单机会可承担上限；原止损尺度、风险缓冲、分组保证金和价格上限继续参与计算。真实预约、持仓及每日累计用量需要组合回测，本研究没有模拟这些组合。各机会的金额不能相加称为账户利润。", "",
              "来源、5/15/30分钟、非LC、金融／商品、产品明细、条件重叠和缺失标签均在assessment.json及各窗口归档中。日期成组区间是描述性不确定度，不能消除反复使用同一开发数据的选择偏差。", ""]
    return "\n".join(lines)


def assess():
    proof = verify_freeze()
    plan = json.loads(PLAN.read_text())
    output = ROOT / plan["output"]
    windows, combined, manifests = {}, defaultdict(list), {}
    for month in plan["months"]:
        directory = output / month
        manifest = json.loads((directory / "manifest.json").read_text())
        require(manifest["status"] == "completed", "窗口未完成")
        require(manifest["plan_sha256"] == file_sha256(PLAN) and manifest["freeze_sha256"] == file_sha256(FREEZE), "窗口的声明/实现指纹不符")
        for filename, expected in manifest["files"].items():
            require(file_sha256(directory / filename) == expected, "窗口文件改变：" + filename)
        records = {name: read_records(directory / (name + ".json.gz")) for name in ("observations", "shapes", "matches", "legacy_cases", "shape_lifecycle")}
        windows[month] = assessments(records["observations"], records["shapes"], records["matches"], records["legacy_cases"])
        windows[month]["shape_lifecycle"] = dict(Counter(r["action"] + ":" + r.get("reason", "") for r in records["shape_lifecycle"]))
        for name, rows in records.items():
            combined[name].extend(rows)
        manifests[month] = file_sha256(directory / "manifest.json")
    for filename, expected in proof["prior_result_hashes"].items():
        require(file_sha256(ROOT / filename) == expected, "此前发布结果改变：" + filename)
    assessment = {"schema": 1, "status": "completed", "sample_status": plan["sample_status"],
                  "primary_horizon": 15, "plan_sha256": file_sha256(PLAN), "freeze_sha256": file_sha256(FREEZE),
                  "monthly_manifest_hashes": manifests, "windows": windows,
                  "combined": assessments(combined["observations"], combined["shapes"], combined["matches"], combined["legacy_cases"]),
                  "prior_result_files_verified": len(proof["prior_result_hashes"]), "full_strategy_backtests": 0,
                  "locked_test_read": False, "automatic_promotion": False}
    write_json(output / "assessment.json", assessment)
    (output / "report.md").write_text(render(assessment), encoding="utf-8")
    table = []
    for month, item in windows.items():
        for horizon, groups in item["ranking"].items():
            for group, row in groups.items():
                table.append({"month": month, "horizon": horizon, "rank_group": group,
                              **{k: row[k] for k in ("rows", "trading_days", "economic_complete", "raw_complete")},
                              **{name: estimate["mean"] for name, estimate in row["metrics"].items()}})
    write_csv(output / "ranking_horizons.csv", table)
    with tarfile.open(output / "source_snapshot.tar.gz", "w:gz") as archive:
        for filename in proof["source_hashes"]:
            archive.add(ROOT / filename, arcname=filename)
        archive.add(PLAN, arcname="research/opportunity_quality_plan.json")
        archive.add(FREEZE, arcname="research/opportunity_quality_freeze.json")
    write_json(output / "delivery.json", {"status": "passed", "assessment_sha256": file_sha256(output / "assessment.json"),
        "report_sha256": file_sha256(output / "report.md"), "source_archive_sha256": file_sha256(output / "source_snapshot.tar.gz"),
        "prior_result_files_verified": len(proof["prior_result_hashes"]), "monthly_scans": len(windows),
        "full_strategy_backtests": 0, "locked_test_read": False})
    print(json.dumps({"phase": "assessed", "output": str(output), "days": len(plan["months"]["2026-07"]["trading_days"]) + len(plan["months"]["2026-08"]["trading_days"]) + len(plan["months"]["2026-09"]["trading_days"]),
                      "segments": assessment["combined"]["base"]["rows"], "prior_result_files_verified": len(proof["prior_result_hashes"])}), flush=True)
    return assessment
