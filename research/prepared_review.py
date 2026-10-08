"""Reuse verified causal frames while bounding memory for development reviews.

The original feature algorithm and cache identity are checked against the saved
source archive. Cropping removes raw warmup rows, never recomputes rolling values,
and retains the preceding daily observations used for real-contract selection.
"""

import ast
import gzip
import hashlib
import json
import tarfile
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd
import talib

from .calendar import stamp
from .config import ResearchError, digest
from .data import Bar, DailyObservation, Dataset, file_sha256, json_bytes
from .feature_cache import read_frames
from .signals import Features


def algorithm_nodes(source):
    tree = ast.parse(source)
    frame = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "feature_frame")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Features")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {"latest", "past"}]
    return [ast.dump(n, include_attributes=False) for n in [frame, *methods]]


class PreparedDataset(Dataset):
    def training_fingerprint(self, day):
        if day != self.verified_training_cutoff:
            raise ResearchError("复核只能引用已逐条核对的原训练截止日")
        return self.verified_training_fingerprint


def prepare_review(run, cfg):
    run = Path(run).resolve()
    original = json.loads((run / "config_snapshot.json").read_text())
    window = original["splits"]["validation"]
    if cfg["splits"] != original["splits"] or cfg["metadata"] != original["metadata"] or cfg["calendar"] != original["calendar"]:
        raise ResearchError("复用指标不能改变时间划分、原池元数据或日历")
    for name in ("atr_period", "include_night_indicators"):
        if cfg["strategy"][name] != original["strategy"][name]:
            raise ResearchError("指标参数改变，不能复用原始因果快照")
    with tarfile.open(run / "source_snapshot.tar.gz", "r:gz") as archive:
        member = archive.extractfile("research/signals.py")
        if member is None:
            raise ResearchError("原实验缺少指标算法来源")
        old_source = member.read()
    current_source = Path(__file__).with_name("signals.py").read_bytes()
    if algorithm_nodes(old_source) != algorithm_nodes(current_source):
        raise ResearchError("指标算法已改变，禁止沿用旧缓存")
    reference = json.loads((run / "data_reference.json").read_text())
    source_path = (run / reference["object"]).resolve()
    if file_sha256(source_path) != reference["sha256"]:
        raise ResearchError("原共享行情指纹改变")
    s = original["strategy"]
    cache_key = digest({
        "algorithm": "causal-sma-talib-wilder-complete-session-v1",
        "data": reference["data_fingerprint"],
        "source": hashlib.sha256(old_source).hexdigest(), "talib": talib.__version__,
        "atr": s["atr_period"], "night": s["include_night_indicators"],
        "calendar": original["calendar"], "metadata": original["metadata"],
    })
    cache = Path(original["storage"]["indicator_cache_root"]) / (cache_key + ".jsonl.gz")
    if not cache.exists():
        raise ResearchError("没有与原数据及指标算法相符的完整缓存")
    prefix, bars, daily = defaultdict(lambda: deque(maxlen=40)), [], []
    training_hash = hashlib.sha256(b'{"bars": [')
    training_cutoff = original["splits"]["train"]["end"]
    training_counts, daily_phase = {"bar": 0, "daily": 0}, False
    with gzip.open(source_path, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if header["fingerprint"] != reference["base_fingerprint"]:
            raise ResearchError("共享行情头部不匹配")
        for line in stream:
            item = json.loads(line)
            row = item["row"]
            if row["trading_day"] > window["end"]:
                raise ResearchError("复核来源包括锁定测试，拒绝读取")
            if item["kind"] == "daily" and not daily_phase:
                training_hash.update(b'], "daily": [')
                daily_phase = True
            if item["kind"] == "bar" and daily_phase:
                raise ResearchError("共享行情记录顺序改变")
            if row["trading_day"] <= training_cutoff:
                if training_counts[item["kind"]]:
                    training_hash.update(b", ")
                training_hash.update(json_bytes(row))
                training_counts[item["kind"]] += 1
            if item["kind"] == "daily":
                daily.append(DailyObservation(**row))
            elif item["kind"] == "bar":
                row["datetime"] = stamp(row["datetime"])
                bar = Bar(**row)
                if bar.trading_day < window["start"]:
                    prefix[bar.key].append(bar)
                else:
                    bars.append(bar)
            else:
                raise ResearchError("未知共享行情记录类型")
    for warmup in prefix.values():
        bars.extend(warmup)
    quality = {"errors": [], "warmup_source": "verified_original_complete_indicator_frames", "model_cutoff": window["end"]}
    if not daily_phase:
        raise ResearchError("该复核入口要求原合约日线来源")
    training_hash.update(b"]}")
    actual_training_hash = training_hash.hexdigest()
    if actual_training_hash != cfg["calibration_snapshot"]["train_data_hash"]:
        raise ResearchError("逐条重新核对的原训练数据指纹不符")
    data = PreparedDataset(bars, cfg, quality, daily=daily)
    data.verified_training_cutoff = training_cutoff
    data.verified_training_fingerprint = actual_training_hash
    features = Features.__new__(Features)
    features.data, features.cache_key, features.frames = data, cache_key, {}
    beginning = pd.Timestamp(window["start"], tz="Asia/Shanghai")
    ending = pd.Timestamp(window["end"], tz="Asia/Shanghai") + pd.Timedelta(days=1)
    for key, minutes, records in read_frames(cache, cache_key):
        frame = pd.DataFrame(records)
        if not frame.empty:
            frame.index = pd.DatetimeIndex(frame.pop("end"))
            left = max(0, frame.index.searchsorted(beginning) - 40)
            right = frame.index.searchsorted(ending)
            frame = frame.iloc[left:right].copy()
            for column in frame.columns:
                if column not in {"datetime", "day", "period"}:
                    frame[column] = frame[column].astype(float)
        features.frames[(key, minutes)] = frame
    evidence = {
        "source_run": str(run), "shared_source_sha256": reference["sha256"],
        "source_data_fingerprint": reference["data_fingerprint"],
        "retained_data_fingerprint": data.fingerprint, "raw_rows_retained": len(data.bars),
        "cache": str(cache), "cache_key": cache_key, "cache_sha256": file_sha256(cache),
        "feature_algorithm_and_accessors_unchanged": True,
        "rolling_indicators_recomputed": False, "locked_test_read": False,
        "training_fingerprint_recomputed_from_full_original_rows": actual_training_hash,
        "original_training_rows_checked": training_counts,
        "sample_status": "development_sample_repeatedly_inspected",
    }
    return data, features, evidence
