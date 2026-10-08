"""Bounded writes and immutable shared research data; no trading dependencies."""

import gzip
import io
import json
import os
import shutil
import stat as stat_type
import uuid
from pathlib import Path

from .config import ResearchError
from .data import Bar, DailyObservation, Dataset, file_sha256


def directory_bytes(roots):
    total = 0
    unique = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for base, _, names in os.walk(root, followlinks=False):
            for name in names:
                try:
                    stat = (Path(base) / name).stat(follow_symlinks=False)
                    if stat_type.S_ISLNK(stat.st_mode):
                        continue
                except FileNotFoundError:
                    continue  # Temporary journals may disappear during an unrelated test/write.
                identity = stat.st_dev, stat.st_ino
                if identity not in unique:
                    total += stat.st_size
                    unique.add(identity)
    return total


class SpaceBudget:
    def __init__(self, policy):
        self.policy = policy
        self.roots = [Path(p).resolve() for p in policy["roots"]]
        if not self.roots or policy["max_bytes"] <= 0 or policy["min_free_bytes"] < 0:
            raise ResearchError("磁盘预算配置无效")

    def check(self, target=None, reserve=0):
        target = Path(target or self.roots[0]).resolve()
        if not any(target.is_relative_to(root) for root in self.roots):
            raise ResearchError(f"写入路径不在本轮预算目录内：{target}")
        usage = directory_bytes(self.roots)
        probe = target
        while not probe.exists():
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if usage + reserve > self.policy["max_bytes"]:
            raise ResearchError("达到本轮新增空间预算，停止并保留已完成文件")
        if free - reserve < self.policy["min_free_bytes"]:
            raise ResearchError("剩余磁盘低于安全余量，停止并保留已完成文件")
        return {
            "used_bytes": usage,
            "free_bytes": free,
            "max_bytes": self.policy["max_bytes"],
            "minimum_free_bytes": self.policy["min_free_bytes"],
        }


class BoundedFile(io.RawIOBase):
    """Charge every raw write, including compressed output and temporary objects."""

    def __init__(self, stream, budget, path):
        self.stream, self.budget, self.path = stream, budget, path

    def write(self, value):
        if not value:
            return 0  # gzip often emits no bytes; do not rescan every file for an empty write.
        if self.budget:
            self.budget.check(self.path, reserve=len(value))
        written = self.stream.write(value)
        self.stream.flush()
        return written

    def writable(self):
        return True

    def flush(self):
        return self.stream.flush()


def write_gzip_json(path, value, budget=None):
    path = Path(path)
    if budget:
        budget.check(path)
    with path.open("wb") as raw:
        # Buffer compressed bytes, so each actual disk write still receives the
        # exact budget check without walking all archives for small gzip chunks.
        with io.BufferedWriter(BoundedFile(raw, budget, path), buffer_size=1024 * 1024) as sink:
            with gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=sink) as zipped:
                with io.TextIOWrapper(zipped, encoding="utf-8") as text:
                    json.dump(value, text, ensure_ascii=False, allow_nan=False)


def write_bounded_json(path, value, budget):
    """Stream JSON in bounded chunks instead of constructing a second full string."""
    path = Path(path)
    budget.check(path)
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("wb") as stream:
        # As with compressed reports, validate every actual disk write while
        # avoiding a full archive scan for each small JSON encoding chunk.
        with io.BufferedWriter(BoundedFile(stream, budget, path), buffer_size=1024 * 1024) as sink:
            pending, length = [], 0
            for piece in encoder.iterencode(value):
                pending.append(piece)
                length += len(piece)
                if length >= 32768:
                    sink.write("".join(pending).encode())
                    pending, length = [], 0
            if pending:
                sink.write("".join(pending).encode())


def read_result(directory):
    directory = Path(directory)
    legacy_or_failed = directory / "result.json"
    if legacy_or_failed.exists():
        return json.loads(legacy_or_failed.read_text())
    compressed = directory / "result.json.gz"
    if compressed.exists():
        with gzip.open(compressed, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    raise ResearchError("实验结果尚未完整保存")


def store_dataset(data, run, root, expected_fingerprint, budget=None):
    """One base snapshot for all K/entry/scope variants of the same causal cutoff."""
    root, run = Path(root).resolve(), Path(run).resolve()
    if budget:
        budget.check(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / (data.fingerprint + ".jsonl.gz")
    published = not target.exists()
    if not target.exists():
        temporary = root / (target.name + "." + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as raw:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    mtime=0,
                    fileobj=BoundedFile(raw, budget, temporary),
                ) as zipped:
                    with io.TextIOWrapper(zipped, encoding="utf-8") as stream:
                        stream.write(
                            json.dumps({"schema": 1, "fingerprint": data.fingerprint})
                            + "\n"
                        )
                        for kind, rows in (("bar", data.bars), ("daily", data.daily)):
                            for row in rows:
                                stream.write(
                                    json.dumps(
                                        {"kind": kind, "row": row.wire()},
                                        ensure_ascii=False,
                                        allow_nan=False,
                                    )
                                    + "\n"
                                )
            # Exclusive publication: a completed object is never silently replaced.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if file_sha256(target) != file_sha256(temporary):
                    raise ResearchError("共享数据对象已存在但内容不一致") from None
        finally:
            if temporary.exists():
                temporary.unlink()  # Only this attempt's unusable temporary file.
    sha = file_sha256(target)
    checksum = target.with_suffix(target.suffix + ".sha256")
    if checksum.exists():
        if checksum.read_text().strip() != sha:
            raise ResearchError("共享数据对象损坏；不自动覆盖历史行情")
    else:
        if not published:
            raise ResearchError("共享数据对象缺少原始校验文件，不自动采纳未知对象")
        # First publication writes a stable physical-file checksum.
        checksum.write_text(sha + "\n")
    reference = {
        "schema": 1,
        "object": os.path.relpath(target, run),
        "sha256": sha,
        "bytes": target.stat().st_size,
        "base_fingerprint": data.fingerprint,
        "data_fingerprint": expected_fingerprint,
    }
    if budget:
        budget.check(run / "data_reference.json", reserve=4096)
    (run / "data_reference.json").write_text(json.dumps(reference, indent=2))
    return reference


def restore_dataset(run, cfg):
    """Check immutable object integrity and projected data, with legacy snapshot support."""
    from .calendar import stamp

    run = Path(run)
    reference_path = run / "data_reference.json"
    if reference_path.exists():
        reference = json.loads(reference_path.read_text())
        path = (run / reference["object"]).resolve()
        if file_sha256(path) != reference["sha256"]:
            raise ResearchError("共享行情 SHA256 不匹配，拒绝重建报告")
        bars, daily = [], []
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            header = json.loads(next(stream))
            if header != {"schema": 1, "fingerprint": reference["base_fingerprint"]}:
                raise ResearchError("共享行情版本/指纹不匹配")
            for line in stream:
                record = json.loads(line)
                row = record["row"]
                if record["kind"] == "bar":
                    bars.append(Bar(**{**row, "datetime": stamp(row["datetime"])}))
                elif record["kind"] == "daily":
                    daily.append(DailyObservation(**row))
                else:
                    raise ResearchError("未知共享行情记录类型")
        base = Dataset(bars, cfg, daily=daily)
        if base.fingerprint != reference["base_fingerprint"]:
            raise ResearchError("共享行情语义指纹不匹配")
        keys = set(base.metadata.records)
        data = Dataset(
            [b for b in bars if b.key in keys],
            cfg,
            daily=[d for d in daily if d.key in keys],
        )
        if data.fingerprint != reference["data_fingerprint"]:
            raise ResearchError("实验投影行情指纹不匹配")
        return data
    snapshot = run / "normalized_data.json.gz"
    if snapshot.exists():
        with gzip.open(snapshot, "rt", encoding="utf-8") as stream:
            rows = json.load(stream)
        return Dataset(
            [Bar(**{**r, "datetime": stamp(r["datetime"])}) for r in rows], cfg
        )
    return None
