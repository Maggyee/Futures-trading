import copy
import hashlib
import json
import math
from pathlib import Path


class ResearchError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def read_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    embedded = all(isinstance(cfg[key], dict) for key in ("metadata", "calendar"))
    for key in ("metadata", "calendar"):
        if isinstance(cfg[key], dict):
            continue
        target = (path.parent / cfg[key]).resolve()
        cfg[key + "_path"] = str(target)
        cfg[key] = json.loads(target.read_text())
    if isinstance(cfg.get("split_lock"), str):
        target = (path.parent / cfg["split_lock"]).resolve()
        cfg["split_lock_path"] = str(target)
        cfg["split_lock"] = json.loads(target.read_text())
    if not embedded:
        cfg["config_path"] = str(path)
    for source in cfg["data"]["sources"] + cfg["data"].get("daily_sources", []):
        if "cloud" in source:
            source["cloud"]["policy"] = str((path.parent / source["cloud"]["policy"]).resolve())
        else:
            source["path"] = str((path.parent / source["path"]).resolve())
    storage = cfg.get("storage", {})
    if storage.get("shared_root"):
        storage["shared_root"] = str((path.parent / storage["shared_root"]).resolve())
    if storage.get("indicator_cache_root"):
        storage["indicator_cache_root"] = str((path.parent / storage["indicator_cache_root"]).resolve())
    if storage.get("budget"):
        storage["budget"]["roots"] = [
            str((path.parent / p).resolve()) for p in storage["budget"]["roots"]
        ]
    validate_config(cfg)
    return cfg


def validate_config(c):
    s, r = c["strategy"], c["risk"]
    check_baseline(c)
    if c.get("execution", {}).get("mode", "formal") not in {"formal", "diagnostic"}:
        raise ResearchError("execution.mode 必须为 formal 或 diagnostic")
    if s["k"] not in [1, 2, 3, 4, 5, 8, 10, "ALL"]:
        raise ResearchError("K 必须为 1/2/3/4/5/8/10/ALL")
    if s["entry_mode"] not in {
        "direct",
        "pullback_ma10",
        "pullback_ma20",
        "pullback_either",
    }:
        raise ResearchError("未知 entry_mode；宽松 MA20 版本不属于严格基准")
    if s["ma40_mode"] not in {"cross", "approach"}:
        raise ResearchError("未知 MA40 模式")
    if not isinstance(s.get("recheck_entry_price", False), bool):
        raise ResearchError("recheck_entry_price 必须为布尔值")
    if s.get("volume_exit_mode", "threshold") not in {"threshold", "weakness"}:
        raise ResearchError("volume_exit_mode 必须为 threshold 或 weakness")
    if s.get("trailing_exit") is not None:
        rule = s["trailing_exit"]
        if not isinstance(rule, dict) or set(rule) != {"activation", "atr_multiple"} or rule["activation"] != "original_target":
            raise ResearchError("追踪止盈必须声明原目标启动及ATR倍数")
        multiple = rule["atr_multiple"]
        if type(multiple) not in (int, float) or not math.isfinite(multiple) or multiple <= 0:
            raise ResearchError("追踪ATR倍数必须为有限正数")
        if c.get("price_replay"):
            raise ResearchError("追踪止盈只支持组合回测，不用于规则价格回放")
    if s.get("entry_cost_filter") is not None:
        rule = s["entry_cost_filter"]
        if not isinstance(rule, dict) or set(rule) != {"max_cost_atr"} or type(rule["max_cost_atr"]) not in (int, float) or not math.isfinite(rule["max_cost_atr"]) or rule["max_cost_atr"] <= 0:
            raise ResearchError("成本ATR上限必须为有限正数")
    if s.get("breakeven") is not None:
        rule = s["breakeven"]
        if not isinstance(rule, dict) or set(rule) != {"activation_r", "include_costs"} or rule["include_costs"] is not True or type(rule["activation_r"]) not in (int, float) or not math.isfinite(rule["activation_r"]) or rule["activation_r"] <= 0 or not s.get("trailing_exit"):
            raise ResearchError("保本规则必须声明正R、覆盖费用并保留原追踪")
    if s.get("afternoon_rerank") is not None:
        if s["afternoon_rerank"] != {"opening_minutes": 8, "rank_by": "afternoon_return", "replace": True}:
            raise ResearchError("午后选品仅支持完成8分钟后替换候选")
    if type(s.get("block_same_day_reentry_after_stop", False)) is not bool:
        raise ResearchError("同日同方向止损后禁入开关必须为布尔值")
    if s.get("entry_confirmation") is not None:
        if s["entry_confirmation"] != {
            "quality_minutes": 5, "valid_minutes": 5, "breakout_lookback_bars": 2,
            "confirmation_bars": 2, "confirmation": "close_beyond_setup_extreme",
            "pullback_reference": "ma10", "preserve_original_channel": True,
            "higher_efficiency_min": 0.45, "require_touch_start_after_armed": True,
        } or s["entry_mode"] != "direct" or s.get("trend_entry") or s.get("dual_entry") or not all(s.get(k) for k in ("slope_band", "trend_quality", "entry_cost_filter")):
            raise ResearchError("延续确认仅支持预声明的高周期资格、相邻两根确认和原通道优先")
    if s.get("structure_protection") is not None:
        if s["structure_protection"] != {
            "timeframe_minutes": 5, "lookback_bars": 3, "buffer_ticks": 1,
            "max_stop_atr": 2.0, "same_session_only": True,
            "retain_original_distance_floors": True, "frozen_after_fill": True,
        } or not s.get("protection_scale") or s.get("candidate_pool") or s.get("candidate_replacement"):
            raise ResearchError("结构保护仅支持同小节三根完成5分钟结构、原风险下限及2ATR上限")
    if (s.get("entry_confirmation") or s.get("structure_protection")) and (
            c.get("execution", {}).get("mode", "formal") != "diagnostic" or c.get("price_replay")):
        raise ResearchError("确认入场与结构保护仅用于声明的离线诊断")
    if s.get("candidate_replacement") is not None:
        if s["candidate_replacement"] != {
            "policy": "skip_known_zero_capacity", "preserve_original_rank": True,
            "refresh": "completed_minute", "preserve_held_and_pending_slots": True,
        } or s["k"] != 2:
            raise ResearchError("候选补位必须保留原排名、K=2及已持仓/预占名额")
    if s.get("candidate_pool") is not None:
        if s["candidate_pool"] != {
            "policy": "executable_affordable_cost", "preserve_original_rank": True,
            "refresh": "completed_minute", "preserve_held_and_pending_slots": True,
        } or s["k"] != 2 or s.get("candidate_replacement") or not s.get("entry_cost_filter"):
            raise ResearchError("可成交候选池必须保留原排名、K=2和原成本上限")
    if s.get("dual_entry") is not None:
        if s["dual_entry"] != {"priority": "direct", "shared_capital": True,
                               "consume_pullback_once": True} or s["entry_mode"] != "direct" or not s.get("trend_entry"):
            raise ResearchError("双通道必须优先突破、共享资金且回踩仅消费一次")
    if s.get("trend_entry") is not None:
        if s["trend_entry"] != {
            "quality_minutes": 5, "valid_minutes": 5,
            "require_touch_after_armed": True,
            "cancel_on_invalid_higher_trend": True,
            "efficiency_min": 0.45, "remove_low_period_slope_gate": True,
        } or not (s["entry_mode"] == "pullback_ma10" or
                  (s["entry_mode"] == "direct" and s.get("dual_entry"))):
            raise ResearchError("趋势观察窗口只支持冻结的5分钟资格与MA10回踩")
    if type(s.get("ma40_exit_confirmation_bars", 1)) is not int or s.get("ma40_exit_confirmation_bars", 1) not in (1, 2):
        raise ResearchError("MA40离场确认只能使用1或2根完成分钟K线")
    if s.get("trend_quality") is not None:
        rule = s["trend_quality"]
        if not isinstance(rule, dict) or set(rule) != {"min_price_changes", "min_displacement_atr", "min_displacement_ticks"}:
            raise ResearchError("趋势质量规则字段不完整")
        if type(rule["min_price_changes"]) is not int or not 1 <= rule["min_price_changes"] <= 10:
            raise ResearchError("趋势质量变化次数必须为1—10的整数")
        if any(type(rule[k]) not in (int, float) or not math.isfinite(rule[k]) or rule[k] <= 0 for k in ("min_displacement_atr", "min_displacement_ticks")):
            raise ResearchError("趋势位移下限必须为有限正数")
    if s.get("protection_scale") is not None:
        scale = s["protection_scale"]
        if not isinstance(scale, dict) or set(scale) != {"atr_multiple", "roundtrip_cost_multiple"} or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scale.values()):
            raise ResearchError("逐笔保护的ATR/成本尺度必须为有限正数")
    if s.get("slope_band") is not None:
        band = s["slope_band"]
        if not isinstance(band, dict) or set(band) != {"timeframes", "min_move_ticks"}:
            raise ResearchError("斜率区间字段不完整")
        floor = band["min_move_ticks"]
        if type(floor) not in (int, float) or not math.isfinite(floor) or floor <= 0:
            raise ResearchError("斜率最小跳数必须为有限正数")
        periods = band["timeframes"]
        if not isinstance(periods, dict) or set(periods) != {"1m", "5m"}:
            raise ResearchError("斜率区间必须同时声明1m和5m")
        for rule in periods.values():
            if not isinstance(rule, dict) or set(rule) != {"lookback_bars", "min_atr_per_bar", "max_atr_per_bar"}:
                raise ResearchError("斜率周期规则字段不完整")
            if type(rule["lookback_bars"]) is not int or not 1 <= rule["lookback_bars"] <= 20:
                raise ResearchError("斜率回看必须为1—20根完成K线")
            low, high = rule["min_atr_per_bar"], rule["max_atr_per_bar"]
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high)) or not 0 < low < high:
                raise ResearchError("斜率上下限必须为有限正数且下限小于上限")
    if s["enable_night"]:
        raise ResearchError(
            "夜盘选品扩展预留，本阶段不支持启用；夜盘指标预热可单独配置"
        )
    if c["data"]["timestamp"] not in {"start", "end"}:
        raise ResearchError("必须声明分钟起始/结束时间")
    if c["data"]["counter_mode"] not in {"incremental", "cumulative"}:
        raise ResearchError("必须声明 volume/turnover 计数口径")
    if c["data"].get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ResearchError("标准时间必须为 Asia/Shanghai")
    if not 0 < r["trade_risk_fraction"] <= r["portfolio_risk_fraction"] < 1:
        raise ResearchError("单笔/组合风险比例无效")
    if not 0 < r["margin_fraction"] <= 1 or r["max_positions"] < 1:
        raise ResearchError("保证金或持仓上限无效")
    if (
        not all(0 <= x <= 1 for x in r["group_fractions"].values())
        or sum(r["group_fractions"].values()) > 1
    ):
        raise ResearchError("分组资金预留比例之和不得大于 1")
    if s["atr_period"] < 2 or s["cooldown_minutes"] < 0 or s["slippage_ticks"] < 0:
        raise ResearchError("ATR/冷却/滑点参数无效")
    if (
        r["initial_capital"] <= 0
        or r["max_lots_per_contract"] < 1
        or r["cost_buffer_multiple"] < 0
    ):
        raise ResearchError("初始资金/手数/成本缓冲无效")
    if s["volume_exit_multiple"] <= 0 or s["shock_max"] <= 0 or s["extension_max"] <= 0:
        raise ResearchError("放量/异常幅度/乖离阈值必须为正")
    days = c["calendar"]["trading_days"]
    if days != sorted(set(days)):
        raise ResearchError("日历交易日必须去重、升序")
    splits = c.get("splits", {})
    for name, w in splits.items():
        if w["start"] > w["end"]:
            raise ResearchError(f"{name} 时间范围无效")
    if {"train", "validation", "test"} <= splits.keys():
        if (
            not splits["train"]["end"] < splits["validation"]["start"]
            or not splits["validation"]["end"] < splits["test"]["start"]
        ):
            raise ResearchError("训练/验证/锁定测试必须严格按时间分离")
    if lock := c.get("split_lock"):
        if lock.get("schema") != 1 or lock.get("kind") != "research_split_only":
            raise ResearchError("未知时间划分锁；不是最终策略冻结文件")
        original = lock["splits"]
        if splits != original:
            fold = c.get("walk_forward_window")
            allowed = c.get("experiments", {}).get("windows", [])
            if not (
                fold in allowed
                and splits == {**original, **fold}
                and original["train"]["start"]
                <= fold["train"]["start"]
                <= fold["train"]["end"]
                < fold["validation"]["start"]
                <= fold["validation"]["end"]
                <= original["validation"]["end"]
            ):
                raise ResearchError("已锁定训练/验证/测试划分，拒绝移动边界")
        observed_days = {
            name: [d for d in days if w["start"] <= d <= w["end"]]
            for name, w in original.items()
        }
        if observed_days != lock["trading_days"]:
            raise ResearchError("划分锁对应的交易日历已改变，须另立版本复核")


def changed(cfg, **kwargs):
    result = copy.deepcopy(cfg)
    result["strategy"].update(kwargs)
    validate_config(result)
    return result


def check_baseline(cfg):
    """An explicit user baseline cannot silently inherit a different default."""
    expected = cfg.get("baseline_expectation")
    if expected is None:
        return
    if expected.get("schema") != 1 or set(expected.get("strategy", {})) != {
        "k", "entry_mode"
    }:
        raise ResearchError("固定基准声明必须包含K和entry_mode")
    observed = {k: cfg["strategy"][k] for k in expected["strategy"]}
    if observed != expected["strategy"]:
        raise ResearchError(f"实际策略不符合固定基准：{observed}，要求{expected['strategy']}")


def execution_gaps(cfg, products):
    gaps = []
    gaps.extend(cfg.get("research_blockers", []))
    if not cfg["calendar"].get("verified", False) and not cfg.get("synthetic", False):
        gaps.append("历史交易日历未核实：calendar.verified")
    for product in sorted(products):
        ticks = cfg["strategy"]["fixed_ticks"].get(product, {})
        if any(
            not isinstance(ticks.get(k), int) or ticks[k] <= 0
            for k in ("stop_loss_ticks", "take_profit_ticks")
        ):
            gaps.append(f"{product}: 缺少正整数固定止损/止盈跳数")
    for contract in cfg["metadata"]["contracts"]:
        if contract["product"] not in products:
            continue
        for key in (
            "tick_size",
            "value_per_price",
            "turnover_factor",
            "listed",
            "expiry",
            "effective_from",
            "margin_rate",
        ):
            if contract.get(key) is None:
                gaps.append(f"{contract['symbol']}.{contract['exchange']}: 缺少 {key}")
        for key in ("tick_size", "value_per_price", "turnover_factor", "margin_rate"):
            if contract.get(key) is not None and contract[key] <= 0:
                gaps.append(f"{contract['symbol']}: {key} 必须为正")
        if not contract.get("fees"):
            gaps.append(f"{contract['symbol']}: 缺少分开仓/平今/平昨的历史手续费")
        for fee in contract.get("fees", []):
            if not all(
                k in fee
                for k in ("effective_from", "open", "close_today", "close_yesterday")
            ):
                gaps.append(f"{contract['symbol']}: 手续费生效日期/开平规则不完整")
            for side in ("open", "close_today", "close_yesterday"):
                item = fee.get(side, {})
                if (
                    item.get("mode") not in {"fixed", "rate"}
                    or not isinstance(item.get("value"), (int, float))
                    or item.get("value", -1) < 0
                ):
                    gaps.append(f"{contract['symbol']}: {side} 费用无效")
        if not contract.get("verified", False) and not cfg.get("synthetic", False):
            gaps.append(f"{contract['symbol']}: 合约元数据未核实")
    return sorted(set(gaps))
