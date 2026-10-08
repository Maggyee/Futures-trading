import argparse
import copy
import json
import sys
from pathlib import Path

from .config import ResearchError, digest, read_config
from .data import export_csv, load_data
from .experiments import (
    apply_calibration,
    calibrate_ticks,
    check_frozen,
    freeze_run,
    run_one,
    sweep,
    walk_forward,
)
from .fixtures import create_fixture
from .reporting import quality_report, report_run, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="开盘强弱 + 多周期波段离线研究（不会导入/连接 CTP）"
    )
    parser.add_argument(
        "command",
        choices=[
            "acquire-month",
            "report-acquisition",
            "audit-month",
            "review-costs",
            "prepare-diagnostic",
            "extend-diagnostic",
            "diagnostic-backtest",
            "prepare-price-replay",
            "price-replay",
            "validate-data",
            "backtest",
            "sweep-topk",
            "compare-entry",
            "walk-forward",
            "report",
            "calibrate-ticks",
            "sensitivity",
            "make-fixture",
            "freeze",
        ],
    )
    parser.add_argument("--config", default="research/examples/config.json")
    parser.add_argument("--output", default="research_outputs")
    parser.add_argument("--run")
    parser.add_argument("--source-dir", help="已归档官方历史费用/合约参数目录")
    parser.add_argument(
        "--execution-review", help="价格回放可选的已复核诊断配置，仅引用报价步长证据"
    )
    parser.add_argument(
        "--data-run", help="复用已有实验的不可变共享行情，校验物理与语义指纹"
    )
    parser.add_argument(
        "--fetch-official",
        action="store_true",
        help="串行归档已核对的上期所公开资料；review-costs仅参考，prepare-diagnostic生成独立诊断配置",
    )
    parser.add_argument(
        "--split", choices=["train", "validation", "test"], default="validation"
    )
    parser.add_argument(
        "--scope", choices=["commodity", "financial", "shared"], default="shared"
    )
    parser.add_argument("--budget", type=int)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="显式授权仅工程测试合成样例；结果不能作为策略验证",
    )
    parser.add_argument("--calibration")
    parser.add_argument("--tick-index", type=int, default=0)
    parser.add_argument("--frozen")
    parser.add_argument(
        "--phase", choices=["catalogue", "daily", "minute", "all"], default="all"
    )
    parser.add_argument(
        "--requests", type=int, help="本次最多新增的匿名请求；再次运行可继续"
    )
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    try:
        if args.command == "extend-diagnostic":
            from .exchange_diagnostic import extend_diagnostic

            if not args.source_dir:
                raise ResearchError(
                    "extend-diagnostic需要 --source-dir 完整公开归档和规格复核文件"
                )
            print(
                json.dumps(
                    extend_diagnostic(args.config, args.source_dir, output),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "prepare-price-replay":
            from .price_replay import prepare_price_replay

            if not args.calibration:
                raise ResearchError("prepare-price-replay需要训练期 --calibration")
            print(
                json.dumps(
                    prepare_price_replay(
                        args.config,
                        args.calibration,
                        output,
                        args.tick_index,
                        args.execution_review,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "prepare-diagnostic":
            from .diagnostic import prepare_diagnostic

            if not args.calibration:
                raise ResearchError("prepare-diagnostic需要训练期 --calibration")
            prepared = prepare_diagnostic(
                args.config,
                args.calibration,
                output,
                args.tick_index,
                args.fetch_official,
            )
            print(json.dumps(prepared, ensure_ascii=False))
            return 0
        if args.command == "review-costs":
            from .cost_review import review_costs

            if not args.source_dir:
                raise ResearchError("review-costs需要 --source-dir 原始参数目录")
            review = review_costs(
                args.config, args.source_dir, output, args.fetch_official
            )
            print(json.dumps(review, ensure_ascii=False))
            return 1 if review["unavailable_days"] else 0
        if args.command in {"acquire-month", "report-acquisition", "audit-month"}:
            from .acquisition import (
                acquire_month,
                acquisition_config,
                report_acquisition,
            )

            if args.command == "audit-month":
                from .readiness import audit_month

                settings = acquisition_config(args.config)
                review = audit_month(settings, output)
                print(json.dumps(review, ensure_ascii=False))
                return 1 if review["reconciliation_errors"] else 0
            if args.command == "report-acquisition":
                settings = acquisition_config(args.config)
                print(report_acquisition(settings["root"], output))
                return 0
            if args.requests is not None and args.requests <= 0:
                raise ResearchError("--requests 必须为正整数")
            progress = acquire_month(args.config, args.phase, args.requests)
            print(
                json.dumps(
                    {
                        "status": progress["status"],
                        "storage": progress.get("storage"),
                        "daily_contracts": len(progress["daily"]),
                        "minute_contracts": len(progress["minute"]),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if progress["status"] != "stopped" else 1
        if args.command == "make-fixture":
            if not args.synthetic:
                raise ResearchError("生成测试样例需 --synthetic")
            create_fixture(output)
            print(output / "config.json")
            return 0
        if args.command == "freeze":
            if not args.run or not args.frozen:
                raise ResearchError("freeze 需要 --run 验证实验目录 --frozen 输出.json")
            freeze_run(args.run, args.frozen)
            print(args.frozen)
            return 0
        if args.command == "report":
            if not args.run:
                raise ResearchError("report 需要 --run 实验目录")
            print(report_run(args.run))
            return 0
        cfg = read_config(args.config)
        from .execution_parameters import execution_mode

        if cfg.get("price_replay") and args.command != "price-replay":
            raise ResearchError(
                "价格回放配置只允许price-replay；不能搜索、资金回测或freeze"
            )
        if args.command == "price-replay" and (
            not cfg.get("price_replay")
            or args.split != "validation"
            or args.scope != "shared"
            or args.calibration
        ):
            raise ResearchError(
                "price-replay仅允许准备后的validation/shared，不重新选候选或进入锁定测试"
            )

        if args.command == "backtest" and execution_mode(cfg) != "formal":
            raise ResearchError(
                "backtest保留正式检查；诊断配置请使用 diagnostic-backtest"
            )
        if args.command == "diagnostic-backtest" and (
            execution_mode(cfg) != "diagnostic"
            or args.split != "validation"
            or args.scope != "shared"
            or args.calibration
        ):
            raise ResearchError(
                "diagnostic-backtest仅允许已准备配置的validation/shared基准，不得重新校准或进入锁定测试"
            )
        if cfg.get("synthetic") and not args.synthetic:
            raise ResearchError("SYNTHETIC_TEST_ONLY 配置需要显式 --synthetic")
        if args.calibration:
            cfg = apply_calibration(
                cfg, json.loads(Path(args.calibration).read_text()), args.tick_index
            )
        if args.command == "backtest" and args.split == "test":
            if not args.frozen:
                raise ResearchError("锁定测试必须提供验证后冻结的 --frozen 文件")
            frozen = json.loads(Path(args.frozen).read_text())
            if args.scope != frozen["scope"]:
                raise ResearchError("最终测试资金场景必须与冻结验证方案一致")
            # Frozen config is authoritative; still enforce same source/risk/strategy in input.
            check_frozen(args.frozen, cfg)
            cfg = copy.deepcopy(frozen["configuration"])
        cutoff = None
        if args.command == "calibrate-ticks":
            cutoff = cfg["splits"]["train"]["end"]
        elif args.command in {"backtest", "diagnostic-backtest", "price-replay"}:
            cutoff = cfg["splits"][args.split]["end"]
        elif args.command in {"sweep-topk", "compare-entry", "sensitivity"}:
            cutoff = cfg["splits"]["validation"]["end"]
        elif args.command == "walk-forward":
            windows = cfg["experiments"]["windows"] or [
                {
                    "train": cfg["splits"]["train"],
                    "validation": cfg["splits"]["validation"],
                }
            ]
            cutoff = max(w["validation"]["end"] for w in windows)
            if cutoff >= cfg["splits"]["test"]["start"]:
                raise ResearchError("滚动窗口禁止读取锁定测试集")
        if args.data_run:
            from .storage import restore_dataset

            if args.command not in {"diagnostic-backtest", "price-replay"}:
                raise ResearchError("--data-run仅供独立诊断或规则价格回放使用")
            source_run = Path(args.data_run)
            saved_manifest = json.loads((source_run / "manifest.json").read_text())
            if saved_manifest["window"]["end"] != cutoff or any(
                r["end"][:10] > cutoff for r in saved_manifest["data_coverage"]
            ):
                raise ResearchError("共享行情快照超出验证截止，拒绝读取未来窗口")
            data = restore_dataset(args.data_run, cfg)
            if (
                data is None
                or any(b.trading_day > cutoff for b in data.bars)
                or any(d.trading_day > cutoff for d in data.daily)
            ):
                raise ResearchError("共享行情缺失或包含窗口之后数据，拒绝读取锁定测试")
            from .data import file_sha256

            quality_path = source_run / "data_quality.json"
            data.quality = json.loads(quality_path.read_text())
            data.quality["shared_snapshot_audit_reference"] = {
                "path": str(quality_path.resolve()),
                "sha256": file_sha256(quality_path),
                "audit_reused": True,
                "source_files_not_downloaded_again": True,
            }
        else:
            data = load_data(cfg, cutoff=cutoff)
        if args.command == "backtest" and args.split == "test":
            expected = [
                (r["path"], r.get("sha256")) for r in frozen["source_fingerprints"]
            ]
            observed = [(r["path"], r.get("sha256")) for r in data.quality["sources"]]
            if expected != observed:
                raise ResearchError(
                    "冻结后的源数据文件已改变，拒绝把测试集变化用于选参"
                )
        if args.command == "validate-data":
            from .calendar import at
            from .signals import rank_candidates
            from .storage import SpaceBudget

            policy = cfg.get("storage", {}).get("budget")
            audit_budget = SpaceBudget(policy) if policy else None
            if policy:
                SpaceBudget(policy).check(output, reserve=4 * 1024 * 1024)
            output.mkdir(parents=True, exist_ok=True)
            write_json(output / "data_quality.json", data.quality, audit_budget)
            pools, exclusions, candidates, ranking_exclusions = [], [], [], []
            for day in data.calendar.days:
                pool, excluded = data.pool(day)
                pools.extend(
                    {k: v for k, v in row.items() if k != "meta"} for row in pool
                )
                exclusions.extend(excluded)
                if day < min(w["start"] for w in cfg["splits"].values()):
                    continue
                ranked, removed = rank_candidates(
                    data, day, pool, at(day, "09:38"), cfg["strategy"]["k"]
                )
                candidates.extend(ranked)
                ranking_exclusions.extend(removed)
            write_json(output / "daily_pool.json", pools, audit_budget)
            write_json(output / "pool_exclusions.json", exclusions, audit_budget)
            write_json(output / "daily_candidates.json", candidates, audit_budget)
            write_json(
                output / "ranking_exclusions.json", ranking_exclusions, audit_budget
            )
            quality_report(output, data)
            if not data.quality["errors"] and cfg.get("storage", {}).get(
                "audit_export_normalized", True
            ):
                export_csv(data, output / "normalized_bars.csv.gz")
            print(
                json.dumps(
                    {
                        "rows": len(data.bars),
                        "errors": len(data.quality["errors"]),
                        "configuration_gaps": data.quality["configuration_gaps"],
                        "output": str(output),
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if data.quality["errors"] else 0
        if args.command == "calibrate-ticks":
            from .storage import SpaceBudget

            policy = cfg.get("storage", {}).get("budget")
            calibration_budget = SpaceBudget(policy) if policy else None
            if calibration_budget:
                calibration_budget.check(output, reserve=1024 * 1024)
            output.mkdir(parents=True, exist_ok=True)
            calibration = calibrate_ticks(
                data.until(cfg["splits"]["train"]["end"]), cfg
            )
            from .experiments import code_identity, dependencies

            calibration["reproducibility"] = {
                **code_identity(),
                "dependencies": dependencies(),
                "configuration_hash": digest(cfg),
                "random_seed": cfg["seed"],
            }
            write_json(
                output / "training_tick_candidates.json",
                calibration,
                calibration_budget,
            )
            print(
                json.dumps(
                    {
                        "path": str(output / "training_tick_candidates.json"),
                        "status": calibration["status"],
                        "ready_products": len(calibration["products"]),
                        "missing_products": sorted(calibration["missing_products"]),
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if calibration["missing_products"] else 0
        elif args.command in {"backtest", "diagnostic-backtest", "price-replay"}:
            directory, result = run_one(
                data,
                cfg,
                output,
                cfg["splits"][args.split],
                args.split,
                args.scope,
                make_report=True,
                price_replay=args.command == "price-replay",
            )
            print(
                json.dumps(
                    {
                        "directory": str(directory),
                        "status": result["status"],
                        "metrics": result.get("metrics"),
                        "path_summary": result.get("path_summary"),
                        "error": result.get("error"),
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if result["status"] == "failed" else 0
        elif args.command in {"sweep-topk", "compare-entry", "sensitivity"}:
            summary = sweep(
                data,
                cfg,
                output,
                {
                    "sweep-topk": "topk",
                    "compare-entry": "entry",
                    "sensitivity": "sensitivity",
                }[args.command],
                args.budget,
            )
            print(
                json.dumps(
                    {
                        "attempted": len(summary["results"]),
                        "completed": sum(
                            r["status"] == "completed" for r in summary["results"]
                        ),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if any(r["status"] == "failed" for r in summary["results"]) else 0
        elif args.command == "walk-forward":
            summary = walk_forward(data, cfg, output, args.budget)
            print(
                json.dumps(
                    {
                        "folds": len(summary["folds"]),
                        "reason": summary["reason"],
                        "output": str(output),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "report":
            if not args.run:
                raise ResearchError("report 需要 --run 实验目录")
            print(report_run(args.run, data))
        return 0
    except (ResearchError, OSError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
