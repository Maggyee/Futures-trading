"""Authenticated, read-only views of offline research artifacts. No trading RPC."""

import json
import os
from pathlib import Path

from fastapi import HTTPException

from .config import ROOT


def research_root():
    return Path(
        os.environ.get("WORKBENCH_RESEARCH_ROOT", ROOT / "research_outputs")
    ).resolve()


def run_directory(relative):
    root = research_root()
    directory = (root / relative).resolve()
    if (
        not directory.is_relative_to(root)
        or not (directory / "manifest.json").is_file()
    ):
        raise HTTPException(404, "研究实验不存在")
    return directory


def list_runs():
    root = research_root()
    rows = []
    if not root.exists():
        return rows
    for path in root.rglob("manifest.json"):
        if not path.resolve().is_relative_to(root):
            continue
        try:
            manifest = json.loads(path.read_text())
            cfg = manifest["configuration"]
            rows.append(
                {
                    "path": path.parent.relative_to(root).as_posix(),
                    "id": manifest["experiment_id"],
                    "created": manifest["created_utc"],
                    "split": manifest["split"],
                    "scope": manifest["scope"],
                    "k": cfg["strategy"]["k"],
                    "entry_mode": cfg["strategy"]["entry_mode"],
                    "synthetic": cfg.get("synthetic", False),
                    "window": manifest["window"],
                }
            )
        except (OSError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda r: (r["created"], r["path"]), reverse=True)


def run_summary(relative):
    directory = run_directory(relative)
    path = directory / "summary.json"
    if not path.exists():
        raise HTTPException(404, "实验摘要尚未生成")
    try:
        result = json.loads(path.read_text())
        manifest = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "研究结果文件暂时不可读") from exc
    return {
        "result": result,
        "coverage": manifest["data_coverage"],
        "code_hash": manifest["code_hash"],
        "data_hash": manifest["data_fingerprint"],
        "artifacts": sorted(p.name for p in directory.iterdir() if p.name in ARTIFACTS),
    }


ARTIFACTS = {
    "report.md",
    "trades.csv",
    "signals.csv",
    "trades.csv.gz",
    "signals.csv.gz",
    "daily_candidates.csv.gz",
    "daily_candidates.csv",
    "data_quality.json",
    "equity.html",
    "drawdown.html",
    "monthly.html",
    "case_profit.html",
    "case_loss.html",
    "case_rejected.html",
}


def artifact(relative, name):
    if name not in ARTIFACTS:
        raise HTTPException(404, "不支持的研究文件")
    directory = run_directory(relative)
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise HTTPException(404, "研究文件不存在")
    return path
