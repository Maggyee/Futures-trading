"""Stream completed indicator frames; memory does not grow with archive size."""

import gzip
import io
import json
import math
import os
import uuid
from pathlib import Path

from .config import ResearchError
from .storage import BoundedFile


def read_frames(path, key):
    """At most one contract/timeframe's records are materialized at once."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        header = json.loads(stream.readline())
        if header != {"schema": 3, "cache_key": key, "compression_level": 6}:
            raise ResearchError("指标缓存版本/指纹不符")
        identity, rows, count, finished = None, [], 0, False
        for line in stream:
            item = json.loads(line)
            if item.get("kind") == "frame":
                if identity or finished:
                    raise ResearchError("指标缓存frame边界无效")
                identity = item["contract"], item["minutes"]
            elif item.get("kind") == "end_frame":
                if not identity or item["rows"] != len(rows):
                    raise ResearchError("指标缓存行数或frame终止标记不符")
                yield *identity, rows
                identity, rows = None, []
                count += 1
            elif item.get("kind") == "complete":
                if identity or item["frames"] != count or finished:
                    raise ResearchError("指标缓存未完整保存")
                finished = True
            elif not identity or finished:
                raise ResearchError("指标缓存记录出现在frame之外")
            else:
                rows.append(item)
        if not finished or identity:
            raise ResearchError("指标缓存没有完整结束标记")


def write_frames(path, key, frames, budget=None):
    """Publish atomically after the gzip trailer and completion marker are written.

    Failures leave a bounded .partial artifact; it can never be mistaken for a
    valid cache. Existing completed caches are immutable and are not overwritten.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    if budget:
        budget.check(temporary, reserve=1024 * 1024)
    with temporary.open("xb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            mtime=0,
            compresslevel=6,
            fileobj=BoundedFile(raw, budget, temporary),
        ) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8") as stream:

                def write(value):
                    stream.write(
                        json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
                    )

                write({"schema": 3, "cache_key": key, "compression_level": 6})
                for (contract, minutes), frame in sorted(frames.items()):
                    write({"kind": "frame", "contract": contract, "minutes": minutes})
                    count = 0
                    for values in frame.itertuples(index=True, name=None):
                        row = {"end": values[0].isoformat()}
                        for column, value in zip(
                            frame.columns, values[1:], strict=True
                        ):
                            row[column] = (
                                value
                                if isinstance(value, str)
                                else float(value)
                                if value is not None and math.isfinite(float(value))
                                else None
                            )
                        write(row)
                        count += 1
                    write({"kind": "end_frame", "rows": count})
                write({"kind": "complete", "frames": len(frames)})
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise ResearchError("指标缓存已存在，不覆盖；保留本次临时文件") from None
    temporary.unlink()
