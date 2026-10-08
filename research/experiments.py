import copy
import hashlib
import importlib.metadata
import json
import math
import subprocess
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import ResearchError, changed, digest, validate_config
from .data import Dataset
from .execution import PortfolioBacktest
from .execution_parameters import ExecutionParameters, execution_mode
from .reporting import (
    funnel,
    metrics,
    rank_contribution,
    report_run,
    write_csv,
    write_json,
)
from .signals import Features
from .storage import SpaceBudget, read_result, store_dataset, write_gzip_json

ROOT = Path(__file__).resolve().parents[1]


def code_identity():
    files = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "research").rglob("*.py"))
    }
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return {
        "code_hash": digest(files),
        "source_hashes": files,
        "git_commit": git.stdout.strip() if git.returncode == 0 else None,
    }


def dependencies():
    return dict(
        sorted(
            (dist.metadata["Name"], dist.version)
            for dist in importlib.metadata.distributions()
        )
    )


def scope_data(data, cfg, scope):
    config = copy.deepcopy(cfg)
    if scope != "shared":
        contracts = [m for m in config["metadata"]["contracts"] if m["group"] == scope]
        config["metadata"]["contracts"] = contracts
        keys = {m["symbol"] + "." + m["exchange"] for m in contracts}
        bars = [b for b in data.bars if b.key in keys]
    else:
        bars = data.bars
        if data.cfg == config:
            return data, config
    # Keep the same group reserves/initial capital in isolated and shared experiments.
    keys = set(
        m["symbol"] + "." + m["exchange"] for m in config["metadata"]["contracts"]
    )
    return Dataset(
        bars, config, data.quality, daily=[d for d in data.daily if d.key in keys]
    ), config


def run_one(
    data, cfg, output, window, split="validation", scope="shared", make_report=False,
    price_replay=False,
    prepared_features=None,
    prepared_entries=None,
    engine_factory=None,
):
    validate_config(cfg)
    if bool(cfg.get("price_replay")) != price_replay:
        raise ResearchError("规则价格回放须使用独立配置及price-replay命令，不能作为资金回测")
    if price_replay and (split != "validation" or scope != "shared"):
        raise ResearchError("价格回放只允许固定validation/shared窗口，不读取锁定测试")
    output = Path(output).resolve()
    storage = cfg.get("storage", {})
    budget = SpaceBudget(storage["budget"]) if storage.get("budget") else None
    if budget:
        budget.check(output, reserve=1024 * 1024)
    output.mkdir(parents=True, exist_ok=True)
    np.random.seed(cfg["seed"])
    base_data = (
        data
        if all(b.trading_day <= window["end"] for b in data.bars)
        and all(d.trading_day <= window["end"] for d in data.daily)
        else data.until(window["end"])
    )
    data, cfg = scope_data(base_data, cfg, scope)
    identity = code_identity()
    signature = digest(
        {
            "config": cfg,
            "data": data.fingerprint,
            "window": window,
            "split": split,
            "scope": scope,
        }
    )[:12]
    sequence = 1
    while True:
        run_id = f"run_{sequence:04d}_{signature}"
        directory = output / run_id
        try:
            directory.mkdir()
            break
        except FileExistsError:
            sequence += 1
    manifest = {
        "experiment_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": cfg,
        "configuration_hash": digest(cfg),
        "data_fingerprint": data.fingerprint,
        "split": split,
        "scope": scope,
        "window": window,
        "research_type": "hypothetical_rule_price_replay" if price_replay else execution_mode(cfg),
        "baseline_actual": {k: cfg["strategy"][k] for k in ("k", "entry_mode")},
        "data_coverage": [
            {
                "contract": key,
                "product": bars[0].product,
                "rows": len(bars),
                "start": bars[0].datetime.isoformat(),
                "end": bars[-1].end.isoformat(),
            }
            for key, bars in sorted(data.by_contract.items())
        ],
        "dependencies": dependencies(),
        "random_seed": cfg["seed"],
        **identity,
    }
    write_json(directory / "manifest.json", manifest, budget)
    write_json(directory / "config_snapshot.json", cfg, budget)
    write_json(directory / "data_quality.json", data.quality, budget)
    manifest["data_reference"] = store_dataset(
        base_data,
        directory,
        storage.get("shared_root", output / "datasets"),
        data.fingerprint,
        budget,
    )
    write_json(directory / "manifest.json", manifest, budget)
    with tarfile.open(directory / "source_snapshot.tar.gz", "w:gz") as archive:
        for name in identity["source_hashes"]:
            archive.add(ROOT / name, arcname=name)
    try:
        cache = storage.get("indicator_cache_root", output / "indicator_cache")
        if price_replay:
            from .price_replay import PriceReplayParameters, RulePriceReplay

            parameters, engine = PriceReplayParameters(cfg, data.metadata), RulePriceReplay
        else:
            parameters, engine = ExecutionParameters(cfg, data.metadata), PortfolioBacktest
        if engine_factory is not None:
            if price_replay or scope != "shared" or prepared_features is None:
                raise ResearchError("研究扩展引擎仅用于已核验指标的shared组合回放")
            engine = engine_factory
        parameters.preflight({b.product for b in data.bars}, window["start"], window["end"], data)
        if prepared_features is not None:
            if price_replay or scope != "shared":
                raise ResearchError("已核对的指标仅用于shared组合研究")
            prepared_features.data = data
            backtest = engine(data, cfg, cache, features=prepared_features)
        else:
            backtest = engine(data, cfg, cache)
        if storage.get("stream_signals", False):
            from .signal_journal import CompressedSignalJournal, SignalJournal
            journal = CompressedSignalJournal if storage.get("compress_signal_journal") else SignalJournal
            backtest.signals = journal(directory / "signals_buffer.partial", budget)
            for name in getattr(backtest, "extra_journals", ()):
                setattr(backtest, name, journal(directory / (name + "_buffer.partial"), budget))
        if prepared_entries is not None:
            if prepared_features is None or price_replay or scope != "shared":
                raise ResearchError("入场快照复用要求已验证指标及组合研究")
            with prepared_entries.bind(backtest.logic) as entries:
                backtest.logic = entries
                result = backtest.run(window["start"], window["end"])
                entries.finish()
        else:
            result = backtest.run(window["start"], window["end"])
        if not price_replay:
            result["metrics"] = metrics(result, cfg["risk"]["initial_capital"])
            result["signal_funnel"] = funnel(result["signals"])
            result["rank_contribution"] = rank_contribution(result)
        compact = storage.get("compact_results", False)
        if compact:
            write_gzip_json(directory / "result.json.gz", result, budget)
        else:
            write_json(directory / "result.json", result, budget)
        journal_names = (
            "trades",
            "orders",
            "signals",
            "events",
            "daily_pool",
            "pool_exclusions",
            "daily_candidates",
            "candidate_execution",
            "equity",
            "rank_contribution",
            "candidate_selection",
            "exit_confirmation",
        ) + tuple(getattr(backtest, "extra_journals", ()))
        for name in dict.fromkeys(journal_names):
            if name not in result:
                continue
            if compact:
                write_csv(directory / (name + ".csv.gz"), result[name], budget=budget)
            else:
                write_json(directory / (name + ".json"), result[name], budget)
                write_csv(directory / (name + ".csv"), result[name], budget=budget)
        if "signal_funnel" in result:
            write_json(directory / "signal_funnel.json", result["signal_funnel"], budget)
        write_json(
            directory / "summary.json",
            {
                k: result[k]
                for k in (
                    "status",
                    "start",
                    "end",
                    "provenance",
                    "execution_mode",
                    "execution_qualification",
                    "metrics",
                    "signal_funnel",
                    "unflattened_risk",
                    "break_unflattened_risk",
                    "open_positions",
                    "path_summary",
                    "account_metrics_calculated",
                    "locked_test_read",
                    "unflattened_positions",
                    "break_unflattened_positions",
                )
                if k in result
            },
            budget,
        )
        if make_report:
            report_run(directory, data, result=result)
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
        write_json(directory / "result.json", result, budget)
        write_json(directory / "summary.json", result, budget)
        report_run(directory)
    with (output / "experiments.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {
                    "id": run_id,
                    "config_hash": manifest["configuration_hash"],
                    "window": window,
                    "scope": scope,
                    "status": result["status"],
                    "error": result.get("error"),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return directory, result


def development_window(cfg, window=None):
    window = window or cfg["splits"]["validation"]
    test = cfg["splits"]["test"]
    if window["end"] >= test["start"]:
        raise ResearchError("参数实验禁止读取锁定测试集")
    return window


def calibrate_ticks(data, cfg, train=None):
    train = train or cfg["splits"]["train"]
    if (
        not cfg["splits"]["train"]["start"] <= train["start"] <= train["end"]
        or train["end"] > cfg["splits"]["train"]["end"]
        or train["end"] >= cfg["splits"]["test"]["start"]
    ):
        raise ResearchError("固定跳数校准只能读取声明的训练集")
    if any(b.trading_day > train["end"] for b in data.bars) or any(
        r.trading_day > train["end"] for r in data.daily
    ):
        raise ResearchError(
            "校准器收到训练截止日之后的数据；请先物理截断 Dataset.until(train.end)"
        )
    features = Features(data)
    samples, ticks, diagnostics = {}, {}, {}
    products = sorted({m["product"] for m in cfg["metadata"]["contracts"]})
    acquired_counts = Counter(b.product for b in data.bars)
    selected = {
        (day, row["contract"])
        for day in data.calendar.days
        if train["start"] <= day <= train["end"]
        for row in data.pool(day)[0]
    }
    for product in products:
        diagnostics[product] = {
            "acquired_minutes": acquired_counts[product],
            "selected_training_minutes": 0,
            "positive_volume_training_minutes": 0,
            "invalid_tick_minutes": 0,
            "positive_atr_samples": 0,
        }
    for key in sorted(data.by_contract):
        frame = features.frames[(key, 1)]
        if frame.empty:
            continue
        # Later roll contracts can have warmup inside training dates. They are not
        # calibration observations until previous-day OI actually selects them.
        training = frame.loc[
            (frame.day >= train["start"]) & (frame.day <= train["end"])
        ]
        for row in training.itertuples():
            if (row.day, key) not in selected:
                continue
            meta = data.metadata.get(key, row.day)
            product = meta["product"]
            diagnostic = diagnostics[product]
            diagnostic["selected_training_minutes"] += 1
            if row.volume <= 0:
                continue
            diagnostic["positive_volume_training_minutes"] += 1
            tick = meta.get("tick_size")
            if (
                not isinstance(tick, (int, float))
                or not math.isfinite(tick)
                or tick <= 0
            ):
                diagnostic["invalid_tick_minutes"] += 1
                continue
            if not math.isfinite(row.atr) or row.atr <= 0:
                continue
            samples.setdefault(product, []).append(float(row.atr / tick))
            diagnostic["positive_atr_samples"] += 1
    for product, values in sorted(samples.items()):
        median = float(np.median(values))
        stops = sorted(
            {
                max(1, int(np.ceil(median * m)))
                for m in cfg["experiments"]["stop_atr_multiples"]
            }
        )
        targets = sorted(
            {
                max(1, int(np.ceil(median * m)))
                for m in cfg["experiments"]["target_atr_multiples"]
            }
        )
        ticks[product] = {
            "training_median_atr_ticks": median,
            "training_samples": len(values),
            "stop_candidates": stops,
            "target_candidates": targets,
        }
    missing = {}
    for product in sorted(set(products) - ticks.keys()):
        row = diagnostics[product]
        reasons = []
        if not row["selected_training_minutes"]:
            reasons.append("no_selected_training_minutes")
        elif not row["positive_volume_training_minutes"]:
            reasons.append("no_positive_volume_training_minutes")
        else:
            if row["invalid_tick_minutes"]:
                reasons.append("tick_size_missing_or_invalid")
            if not row["positive_atr_samples"]:
                reasons.append("no_valid_positive_training_atr")
        missing[product] = {"reasons": reasons, **row}
    return {
        "schema": 2,
        "status": "incomplete" if missing else "complete",
        "label": "研究候选，尚未验证最佳值",
        "train_window": train,
        "train_data_hash": data.fingerprint,
        "sampling_policy": "当日按前日OI选中的真实合约，训练段有成交量且ATR有效的完整1分钟线；预热只计算指标",
        "algorithm": "selected-contract-positive-volume-training-atr-v2",
        "atr_algorithm": "TA-Lib Wilder",
        "settings_hash": calibration_settings(cfg),
        "selection_hash": digest(sorted(selected)),
        "locked_test_read": False,
        "products": ticks,
        "required_products": products,
        "missing_products": missing,
        "diagnostics": diagnostics,
    }


def calibration_settings(cfg):
    return digest(
        {
            "atr_period": cfg["strategy"]["atr_period"],
            "include_night_indicators": cfg["strategy"]["include_night_indicators"],
            "stop_atr_multiples": cfg["experiments"]["stop_atr_multiples"],
            "target_atr_multiples": cfg["experiments"]["target_atr_multiples"],
            "algorithm": "selected-contract-positive-volume-training-atr-v2",
        }
    )


def apply_calibration(cfg, calibration, index=0):
    result = copy.deepcopy(cfg)
    if calibration["train_window"]["end"] >= cfg["splits"]["validation"]["start"]:
        raise ResearchError("校准与验证窗口重叠")
    if calibration.get("settings_hash") and calibration[
        "settings_hash"
    ] != calibration_settings(cfg):
        raise ResearchError("校准的ATR/候选生成配置已改变，拒绝沿用候选")
    for product, row in calibration["products"].items():
        if (
            index >= len(row["stop_candidates"])
            or index >= len(row["target_candidates"])
            or index < 0
        ):
            raise ResearchError("校准候选索引无效")
        result["strategy"]["fixed_ticks"][product] = {
            "stop_loss_ticks": row["stop_candidates"][index],
            "take_profit_ticks": row["target_candidates"][index],
        }
    result["calibration_snapshot"] = calibration
    result["calibration_candidate_index"] = index
    return result


def variants(cfg, stage):
    if stage == "topk":
        return [(f"K={k}", changed(cfg, k=k)) for k in cfg["experiments"]["k_values"]]
    if stage == "entry":
        return [
            (mode, changed(cfg, entry_mode=mode))
            for mode in cfg["experiments"]["entry_modes"]
        ]
    items = []
    for parameter, values in (
        ("efficiency_min", cfg["experiments"]["efficiency_values"]),
        ("volume_exit_multiple", cfg["experiments"]["volume_values"]),
        ("ma40_mode", ["cross", "approach"]),
        ("slippage_ticks", cfg["experiments"]["slippage_values"]),
    ):
        items.extend(
            (f"{parameter}={value}", changed(cfg, **{parameter: value}))
            for value in values
        )
    for field in (
        "enable_oi_filter",
        "enable_smooth_filter",
        "enable_multicycle_filter",
        "enable_volume_exit",
        "enable_ma40_exit",
    ):
        items.append(("ablate_" + field, changed(cfg, **{field: False})))
    for profile, clocks in cfg["experiments"].get("time_variants", {}).items():
        for item in clocks:
            experiment = copy.deepcopy(cfg)
            experiment["strategy"]["times"][profile].update(item)
            items.append((f"time_{profile}_{item}", experiment))
    for index, fixed in enumerate(cfg["experiments"].get("fixed_tick_variants", [])):
        experiment = copy.deepcopy(cfg)
        experiment["strategy"]["fixed_ticks"].update(fixed)
        items.append((f"fixed_ticks_{index}", experiment))
    return items


def experiment_budget(cfg, budget=None):
    requested = cfg["experiments"]["max_runs"] if budget is None else budget
    if not isinstance(requested, int) or requested <= 0:
        raise ResearchError("实验预算必须为正整数")
    limit = cfg["experiments"]["max_runs"]
    if not isinstance(limit, int) or limit <= 0:
        raise ResearchError("experiments.max_runs 必须为正整数")
    return min(requested, limit)


def sweep(data, cfg, output, stage="topk", budget=None, scopes=None, window=None):
    if cfg.get("price_replay"):
        raise ResearchError("假设成交价格回放不能用于选K或参数搜索")
    if execution_mode(cfg) == "diagnostic":
        raise ResearchError(
            "本阶段诊断只核对基准执行，不用于选K或参数搜索；正式缺口仍待核实"
        )
    window = development_window(cfg, window)
    data = data.until(window["end"])
    budget = experiment_budget(cfg, budget)
    scopes = scopes or (
        ["commodity", "financial", "shared"] if stage == "topk" else ["shared"]
    )
    rows, attempts = [], []
    for name, experiment in variants(cfg, stage):
        for scope in scopes:
            if len(rows) >= budget:
                attempts.append(
                    {
                        "name": name,
                        "scope": scope,
                        "configuration_hash": digest(experiment),
                        "status": "skipped_budget",
                    }
                )
                continue
            directory, result = run_one(data, experiment, output, window, scope=scope)
            row = {
                "name": name,
                "k": experiment["strategy"]["k"],
                "entry_mode": experiment["strategy"]["entry_mode"],
                "scope": scope,
                "experiment_id": directory.name,
                "status": result["status"],
                **result.get("metrics", {}),
                "rank_contribution": result.get("rank_contribution", []),
                "error": result.get("error"),
            }
            rows.append(row)
            attempts.append(
                {
                    "name": name,
                    "scope": scope,
                    "configuration_hash": digest(experiment),
                    "status": result["status"],
                    "id": directory.name,
                }
            )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    marginal = []
    for scope in scopes:
        previous = None
        for row in [r for r in rows if r["scope"] == scope and "net_profit" in r]:
            ranks = {r["rank"]: r["net_pnl"] for r in row["rank_contribution"]}
            if previous:
                old = {r["rank"]: r["net_pnl"] for r in previous["rank_contribution"]}
                old_k = (
                    previous["k"]
                    if isinstance(previous["k"], int)
                    else max(old, default=0)
                )
                marginal.append(
                    {
                        "scope": scope,
                        "from_k": previous["k"],
                        "to_k": row["k"],
                        "new_rank_net_pnl": sum(
                            v for k, v in ranks.items() if k > old_k
                        ),
                        "existing_rank_net_pnl_change": sum(
                            v - old.get(k, 0) for k, v in ranks.items() if k <= old_k
                        ),
                        "portfolio_net_change": row["net_profit"]
                        - previous["net_profit"],
                    }
                )
            previous = row
    summary = {
        "stage": stage,
        "window": window,
        "locked_test_read": False,
        "attempts": attempts,
        "results": rows,
        "marginal_contribution": marginal,
        "selection": None,
        "warning": "本比较不自动宣布最优K。查看收益/回撤/资金竞争/样本规模与邻近参数；合成数据仅工程验收。",
    }
    if stage == "topk":
        stability = []
        capital = cfg["risk"]["initial_capital"]
        for scope in scopes:
            subset = [
                r for r in rows if r["scope"] == scope and r["status"] == "completed"
            ]
            for left, right in zip(subset, subset[1:], strict=False):
                profit_delta = (right["net_profit"] - left["net_profit"]) / capital
                drawdown_delta = (
                    right["max_drawdown"] - left["max_drawdown"]
                ) / capital
                enough = all(
                    r["daily_count"] >= cfg["experiments"]["minimum_validation_days"]
                    and r["trade_count"]
                    >= cfg["experiments"]["minimum_trades_for_selection"]
                    for r in (left, right)
                )
                flat = (
                    abs(profit_delta) <= cfg["experiments"]["stability_profit_fraction"]
                    and abs(drawdown_delta)
                    <= cfg["experiments"]["stability_drawdown_fraction"]
                )
                stability.append(
                    {
                        "scope": scope,
                        "k_left": left["k"],
                        "k_right": right["k"],
                        "net_delta_fraction": profit_delta,
                        "drawdown_delta_fraction": drawdown_delta,
                        "descriptive_flat_response": flat,
                        "sufficient_sample": enough,
                        "validated_stability": False,
                        "reason": "描述性邻近响应，不是最佳参数验证；少样本或合成数据不能推断稳定性",
                    }
                )
        summary["parameter_stability"] = stability
    name = (
        "top_k_comparison"
        if stage == "topk"
        else "entry_comparison"
        if stage == "entry"
        else "sensitivity"
    )
    write_json(output / (name + ".json"), summary)
    write_csv(output / (name + ".csv"), rows)
    import plotly.graph_objects as go

    chart = go.Figure()
    for scope in scopes:
        group = [r for r in rows if r["scope"] == scope]
        chart.add_trace(
            go.Scatter(
                x=[r["name"] for r in group],
                y=[r.get("net_profit") for r in group],
                name=scope,
                mode="lines+markers",
            )
        )
    chart.update_layout(
        title=("SYNTHETIC_TEST_ONLY · " if cfg.get("synthetic") else "验证集 · ")
        + name
        + "（不能单凭收益选参）"
    )
    chart.write_html(output / (name + ".html"), include_plotlyjs=True)
    (output / (name + ".md")).write_text(
        "# 分阶段参数比较\n\n"
        + summary["warning"]
        + "\n\n"
        + f"窗口 {window}，已执行 {len(rows)} 项，预算未执行 {sum(a['status'] == 'skipped_budget' for a in attempts)} 项。\n"
        + "\n| 配置 | 资金场景 | 状态 | 交易数 | 净盈亏 | 最大回撤 | 风控拒绝 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(
            f"| {r['name']} | {r['scope']} | {r['status']} | {r.get('trade_count')} | {r.get('net_profit')} | {r.get('max_drawdown')} | {r.get('risk_rejection_count')} |"
            for r in rows
        )
        + "\n"
    )
    return summary


def walk_forward(data, cfg, output, budget=None):
    if cfg.get("price_replay"):
        raise ResearchError("假设成交价格回放不能用于滚动选参")
    if execution_mode(cfg) == "diagnostic":
        raise ResearchError("诊断资格不能用于滚动选参")
    windows = cfg["experiments"]["windows"] or [
        {"train": cfg["splits"]["train"], "validation": cfg["splits"]["validation"]}
    ]
    # Validate every window before accessing any data or running an experiment.
    for window in windows:
        if (
            not window["train"]["start"]
            <= window["train"]["end"]
            < window["validation"]["start"]
            <= window["validation"]["end"]
            < cfg["splits"]["test"]["start"]
        ):
            raise ResearchError("滚动窗口重叠或进入锁定测试集")
    remaining = experiment_budget(cfg, budget)
    summaries = []
    for index, window in enumerate(windows):
        if remaining <= 0:
            break
        config = copy.deepcopy(cfg)
        config["splits"].update(window)
        config["walk_forward_window"] = window
        training = data.until(window["train"]["end"])
        calibration = calibrate_ticks(training, config, window["train"])
        config = apply_calibration(
            config,
            calibration,
            config["experiments"].get("calibration_candidate_index", 0),
        )
        destination = Path(output) / f"fold_{index:02d}"
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "training_tick_candidates.json", calibration)
        summary = sweep(
            data.until(window["validation"]["end"]),
            config,
            destination,
            scopes=["shared"],
            budget=remaining,
            window=window["validation"],
        )
        remaining -= len(summary["results"])
        summaries.append(summary)
    valid = [
        r
        for s in summaries
        for r in s["results"]
        if r["status"] == "completed"
        and not cfg.get("synthetic", False)
        and r.get("daily_count", 0) >= cfg["experiments"]["minimum_validation_days"]
        and r.get("trade_count", 0)
        >= cfg["experiments"]["minimum_trades_for_selection"]
    ]
    # Report distributions across folds. Deliberately require an explicit user freeze for final testing.
    result = {
        "folds": summaries,
        "locked_test_read": False,
        "eligible_runs": [r["experiment_id"] for r in valid],
        "selection": None,
        "reason": "需要审阅多指标稳定区间并显式 freeze；样本不足时不宣布最优",
    }
    Path(output).mkdir(parents=True, exist_ok=True)
    write_json(Path(output) / "walk_forward.json", result)
    return result


def freeze_run(directory, destination):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    result = read_result(directory)
    if manifest["split"] != "validation" or result["status"] != "completed":
        raise ResearchError("只能冻结已完成验证集方案，不能用锁定测试结果选参")
    config = manifest["configuration"]
    if config.get("price_replay") or result.get("execution_mode") == "price_replay":
        raise ResearchError("假设成交价格回放不能冻结为账户策略或用于锁定测试")
    if execution_mode(config) == "diagnostic":
        raise ResearchError("诊断执行不是完整验证方案，不能冻结用于锁定测试")
    frozen = {
        "configuration": config,
        "configuration_hash": digest(config),
        "code_hash": manifest["code_hash"],
        "chosen_validation_run": manifest["experiment_id"],
        "frozen_utc": datetime.now(timezone.utc).isoformat(),
        "locked_test": config["splits"]["test"],
        "scope": manifest.get("scope", "shared"),
        "note": "显式冻结研究方案；不是已验证最佳参数",
    }
    frozen["source_fingerprints"] = json.loads(
        (directory / "data_quality.json").read_text()
    )["sources"]
    write_json(destination, frozen)
    return frozen


def check_frozen(path, cfg):
    frozen = json.loads(Path(path).read_text())
    if (
        frozen["configuration_hash"] != digest(frozen["configuration"])
        or frozen["code_hash"] != code_identity()["code_hash"]
    ):
        raise ResearchError("冻结配置/代码指纹不匹配；测试前禁止改参")
    if (
        frozen["configuration_hash"] != digest(cfg)
        or frozen["locked_test"] != cfg["splits"]["test"]
    ):
        raise ResearchError("当前配置与冻结方案不符，请使用冻结验证方案原配置")
    return frozen
