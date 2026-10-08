"""Share identical immutable market files with their local archive copies."""

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
OLD_IN = ROOT / "research_inputs/coverage_expansion_2026-10-05"
OLD_OUT = ROOT / "research_outputs/coverage_expansion_2026-10-05"
STATE = OLD_IN / "cloud_state"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    receipt = HERE / "storage_reuse.json"
    if receipt.exists():
        print(receipt.read_text())
        return
    records, visited = [], set()
    with (STATE / "archive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        targets = [OLD_IN / "datasets", OLD_IN / "2026-07/raw/minute", OLD_IN / "2026-08/raw/minute",
                   OLD_OUT / "2026-07/indicator_cache", OLD_OUT / "2026-08/indicator_cache"]
        for root in targets:
            for path in sorted(root.glob("*")):
                original = path.stat(follow_symlinks=False)
                identity = original.st_dev, original.st_ino
                if not stat.S_ISREG(original.st_mode) or original.st_size < 512 * 1024 or identity in visited:
                    continue
                visited.add(identity)
                checksum = sha(path)
                archived = STATE / "objects" / checksum[:2] / checksum
                if not archived.exists():
                    continue
                before = archived.stat()
                if before.st_ino == original.st_ino or before.st_nlink != 1:
                    continue
                # Archive objects contain public market bytes. The original's
                # ownership and permissions remain authoritative and unchanged.
                if before.st_uid != original.st_uid or before.st_gid != original.st_gid or sha(archived) != checksum:
                    raise RuntimeError("归档副本属性或内容不符")
                temporary = archived.with_name(archived.name + ".link.partial")
                os.link(path, temporary)
                os.replace(temporary, archived)
                after = path.stat()
                if (after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns) != (
                        original.st_mode, original.st_uid, original.st_gid, original.st_mtime_ns) or sha(archived) != checksum:
                    raise RuntimeError("复用后的原始数据或属性改变")
                records.append({"original": str(path), "archive": str(archived), "sha256": checksum,
                                "released_bytes": before.st_size, "original_permissions_preserved": True})
        fcntl.flock(lock, fcntl.LOCK_UN)
    receipt.write_text(json.dumps({"status": "passed", "records": records,
                                  "released_bytes": sum(r["released_bytes"] for r in records)}, indent=2) + "\n")
    print(json.dumps({"files": len(records), "released_bytes": sum(r["released_bytes"] for r in records)}))


if __name__ == "__main__":
    main()
