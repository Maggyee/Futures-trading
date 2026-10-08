"""Keep mutable entry triggers in memory and stream rejected observations to disk."""

import gzip
import io
import json
from pathlib import Path

from .storage import BoundedFile


class SignalJournal(list):
    """List-compatible repeatable iteration for JSON/CSV and funnel exporters.

    Only triggered opportunities can receive later risk/fill updates. Their
    original dictionary references remain live; other rows are final at append.
    """
    def __init__(self, path, budget):
        super().__init__()
        self.path = Path(path)
        self.raw = self.path.open("xb")
        self.sink = io.BufferedWriter(BoundedFile(self.raw, budget, self.path), 1024 * 1024)
        self.live = {}
        self.count = 0

    def append(self, row):
        index = self.count
        self.count += 1
        if row["trigger"]:
            self.live[index] = row
        else:
            self.sink.write((json.dumps([index, row], ensure_ascii=False, allow_nan=False) + "\n").encode())

    def __len__(self):
        return self.count

    def __iter__(self):
        self.sink.flush()
        with self.path.open(encoding="utf-8") as stream:
            record = next(stream, None)
            parsed = json.loads(record) if record else None
            for index in range(self.count):
                if index in self.live:
                    yield self.live[index]
                else:
                    if parsed is None or parsed[0] != index:
                        raise ValueError("信号日志缺失或顺序改变")
                    yield parsed[1]
                    record = next(stream, None)
                    parsed = json.loads(record) if record else None
            if parsed is not None:
                raise ValueError("信号日志包含额外观察")

    def discard(self):
        self.sink.close()
        self.raw.close()
        self.path.unlink()  # Only this run's temporary journal, after durable exports.


class CompressedSignalJournal(SignalJournal):
    """Finalize compressed observations on first export; triggered rows stay mutable."""

    def __init__(self, path, budget):
        super().__init__(path, budget)
        self.zipped = gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=self.sink)
        self.finalized = False

    def append(self, row):
        if self.finalized:
            raise ValueError("不能在已导出的信号日志后追加观察")
        index = self.count
        self.count += 1
        if row["trigger"]:
            self.live[index] = row
        else:
            self.zipped.write((json.dumps([index, row], ensure_ascii=False, allow_nan=False) + "\n").encode())

    def __iter__(self):
        if not self.finalized:
            self.zipped.close()
            self.sink.flush()
            self.finalized = True
        with gzip.open(self.path, "rt", encoding="utf-8") as stream:
            record = next(stream, None)
            parsed = json.loads(record) if record else None
            for index in range(self.count):
                if index in self.live:
                    yield self.live[index]
                else:
                    if parsed is None or parsed[0] != index:
                        raise ValueError("压缩信号日志缺失或顺序改变")
                    yield parsed[1]
                    record = next(stream, None)
                    parsed = json.loads(record) if record else None
            if parsed is not None:
                raise ValueError("压缩信号日志包含额外观察")

    def discard(self):
        if not self.finalized:
            self.zipped.close()
        super().discard()
