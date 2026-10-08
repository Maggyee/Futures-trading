"""Bounded, immutable research backup and verified on-demand cloud reads.

Only the declared research roots are scanned. rclone handles authorization;
this module never reads or archives its credential file. Objects are addressed
by SHA256, checked against Drive's MD5, and published before their manifest.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import gzip
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ResearchError, digest
from .data import file_sha256
from .storage import SpaceBudget, directory_bytes


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class CloudArchive:
    def __init__(self, policy):
        self.policy_path = Path(policy).resolve()
        self.policy = json.loads(self.policy_path.read_text())
        p = self.policy
        if p.get("schema") != 1 or not re.fullmatch(
            r"gdrive:quant_backup/[A-Za-z0-9_./-]+", p["remote"]
        ) or ".." in p["remote"].split("/"):
            raise ResearchError("云端范围必须为quant_backup内的专属研究目录")
        for field in ("cache_max_bytes", "cloud_max_bytes", "transfer_max_bytes_per_day"):
            if type(p[field]) is not int or p[field] <= 0:
                raise ResearchError("云端/缓存/传输限额无效")
        self.state = Path(p["local_state"]).resolve()
        self.cache = Path(p["cache_root"]).resolve()
        self.budget = SpaceBudget(p["local_budget"])
        self.budget.check(self.state, reserve=65536)
        self.state.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.remote = p["remote"].rstrip("/")

    @contextmanager
    def locked(self):
        with (self.state / "archive.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ResearchError("已有云端同步或读取进行中，保留任务等待下一轮") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def command(self, *args, timeout=600):
        command = ["rclone", *map(str, args), "--contimeout", "10s", "--timeout", "60s",
                   "--retries", "2", "--low-level-retries", "2", "--transfers", "1",
                   "--checkers", "2", "--buffer-size", "8M", "--bwlimit", self.policy["bandwidth"]]
        if self.policy.get("rclone_config"):
            command += ["--config", str(Path(self.policy["rclone_config"]).resolve())]
        environment = os.environ.copy()
        if self.policy.get("proxy"):
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                environment[key] = self.policy["proxy"]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False, env=environment)
        if result.returncode:
            # Remote credential/configuration contents are never included in errors.
            raise ResearchError(f"云端操作失败：rclone {args[0]} / exit {result.returncode}")
        return result.stdout

    def charge(self, count):
        ledger_path = self.state / "transfer_ledger.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        day = datetime.now().astimezone().date().isoformat()
        used = ledger.get(day, 0)
        if count < 0 or used + count > self.policy["transfer_max_bytes_per_day"]:
            raise ResearchError("达到本日上传与读取总限额，保留进度等待次日")
        # Reserve before transfer; failures stay charged, never silently retry past quota.
        ledger[day] = used + count
        atomic(ledger_path, ledger)

    def inventory(self):
        listing = self.command("lsjson", self.remote, "--recursive", "--files-only", "--hash", "--fast-list")
        records = json.loads(listing)
        total = sum(r["Size"] for r in records)
        if total > self.policy["cloud_max_bytes"]:
            raise ResearchError("专属云端归档已超过限额，停止新增写入")
        return {r["Path"]: r for r in records}, total

    def _files(self):
        names = set()
        for source in self.policy["sources"]:
            name, root = source["name"], Path(source["root"]).resolve()
            if name in names or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ResearchError("归档源名称重复或无效")
            names.add(name)
            exclusions = set(source.get("exclude", [])) | {"__pycache__"}
            for base, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in exclusions and not d.startswith(".")]
                for filename in sorted(files):
                    path = Path(base) / filename
                    if path.is_symlink() or filename.endswith((".partial", ".pyc")):
                        continue
                    # Dependency pins belong to the replay snapshot; runtime locks do not.
                    if filename.endswith(".lock") and filename != "requirements.lock":
                        continue
                    if filename in {"rclone.conf", ".env"} or "credential" in filename.lower():
                        raise ResearchError("研究目录内出现凭据文件，拒绝归档")
                    relative = str(path.relative_to(root))
                    if source.get("include") is not None and relative not in source["include"]:
                        continue
                    yield name + "/" + relative, path

    def _stage(self, logical, path):
        before = path.stat()
        if before.st_size > min(self.policy["cache_max_bytes"], 512 * 1024**2):
            raise ResearchError("单个归档文件超过512MiB/读取缓存上限")
        sha, md5 = hashlib.sha256(), hashlib.md5()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                sha.update(block); md5.update(block)
        observed = path.stat()
        if (before.st_size, before.st_mtime_ns) != (observed.st_size, observed.st_mtime_ns):
            return None
        key = sha.hexdigest()
        relative = "objects/" + key[:2] + "/" + key
        target = self.state / relative
        record = {"logical_path": logical, "object": relative, "sha256": key,
                  "md5": md5.hexdigest(), "bytes": before.st_size}
        if target.exists():
            if file_sha256(target) != key:
                raise ResearchError("本地归档对象损坏，不覆盖")
            return record  # Verified immutable local objects need no new disk reservation.
        temporary = self.state / "staging.partial"
        self.budget.check(temporary, reserve=before.st_size)
        size = 0
        with path.open("rb") as source, temporary.open("wb") as output:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > before.st_size:
                    # A growing file cannot exceed its pre-reserved allowance.
                    output.close()
                    temporary.unlink()
                    return None
                output.write(block)
        after = path.stat()
        self.budget.check(temporary)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            temporary.unlink()
            return None  # Growing downloads/logs wait until the next snapshot.
        if file_sha256(temporary) != key:
            temporary.unlink()
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_sha256(target) != key:
                raise ResearchError("本地归档对象损坏，不覆盖")
            temporary.unlink()
        else:
            temporary.replace(target)
        return record

    def _pack(self, records, remote):
        if not self.policy.get("pack_objects", False):
            return records
        # Stable SHA-prefix buckets reduce Drive's per-file request overhead.
        # Archive contents are deterministic and every original file retains SHA256.
        buckets, carriers = {}, {}
        status_path = self.state / "status.json"
        if status_path.exists():
            prior = json.loads(status_path.read_text())
            prior_path = self.state / prior["manifest"]
            if prior.get("status") == "verified" and file_sha256(prior_path) == prior["manifest_sha256"]:
                prior_manifest = json.loads(prior_path.read_text())
                if digest(prior_manifest) != prior["manifest_id"]:
                    raise ResearchError("已验证归档的本地清单指纹改变")
                for row in prior_manifest["files"]:
                    if "bundle" in row:
                        carrier = row["bundle"]
                        saved = remote.get(carrier["object"], {})
                        if saved.get("Size") == carrier["bytes"] and saved.get("Hashes",{}).get("md5") == carrier["md5"]:
                            carriers[row["sha256"]] = carrier
        for row in records:
            if row["object"] not in remote and row["sha256"] not in carriers:
                buckets.setdefault(row["sha256"][0], {})[row["sha256"]] = row
        for prefix, members in sorted(buckets.items()):
            groups, group, size = [], [], 0
            for key,row in sorted(members.items()):
                if group and size + row["bytes"] > 480 * 1024**2:
                    groups.append(group); group, size = [], 0
                group.append(row); size += row["bytes"]
            if group:
                groups.append(group)
            for group in groups:
                temporary = self.state / "bundle.partial"
                self.budget.check(temporary, reserve=sum(r["bytes"] + 1024 for r in group) + 65536)
                with temporary.open("wb") as raw:
                    with gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw) as zipped:
                        with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                            for row in group:
                                info = tarfile.TarInfo(row["sha256"])
                                info.size = row["bytes"]
                                info.mode = 0o600
                                with (self.state / row["object"]).open("rb") as value:
                                    archive.addfile(info, value)
                self.budget.check(temporary)
                key = file_sha256(temporary)
                relative = "objects/" + key[:2] + "/" + key
                target = self.state / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("rb") as value:
                    md5 = hashlib.file_digest(value, "md5").hexdigest()
                carrier = {"object": relative, "sha256": key, "bytes": temporary.stat().st_size,
                           "md5": md5}
                if target.exists():
                    if file_sha256(target) != key:
                        raise ResearchError("本地归档包损坏，不覆盖")
                    temporary.unlink()
                else:
                    temporary.replace(target)
                for row in group:
                    carriers[row["sha256"]] = carrier
        return [{**row, **({"bundle": carriers[row["sha256"]], "member": row["sha256"]}
                          if row["sha256"] in carriers else {})} for row in records]

    def sync(self):
        with self.locked():
            self.command("mkdir", self.remote)
            remote, total = self.inventory()
            records = []
            for logical, path in self._files():
                record = self._stage(logical, path)
                if record:
                    records.append(record)
            records.sort(key=lambda x: x["logical_path"])
            records = self._pack(records, remote)
            unique = {r.get("bundle", r)["object"]: r.get("bundle", r) for r in records}
            missing = []
            for name, record in unique.items():
                saved = remote.get(name)
                if saved and (saved["Size"] != record["bytes"] or
                              saved.get("Hashes", {}).get("md5") != record["md5"]):
                    raise ResearchError("云端不可变对象校验失败，不自动覆盖")
                if not saved:
                    missing.append(record)
            by_logical = {r["logical_path"]: r for r in records}
            mirrors, mirror_uploads = [], []
            for logical, destination in self.policy.get("report_mirrors", {}).items():
                if not re.fullmatch(r"reports/[A-Za-z0-9_/-]+\.html", destination) or ".." in destination.split("/"):
                    raise ResearchError("报告副本必须在专属reports目录")
                row = by_logical.get(logical)
                if row is None:
                    continue  # The final report may not have been generated yet.
                mirror = {**row, "destination": destination}
                mirrors.append(mirror)
                saved = remote.get(destination, {})
                if saved.get("Size") != row["bytes"] or saved.get("Hashes", {}).get("md5") != row["md5"]:
                    mirror_uploads.append(mirror)
            manifest = {"schema": 1, "files": records, "policy_hash": digest(self.policy)}
            manifest_id = digest(manifest)
            manifest_path = self.state / "manifests" / (manifest_id + ".json")
            atomic(manifest_path, manifest)
            pointer = {"schema": 1, "manifest": "manifests/" + manifest_path.name,
                       "manifest_sha256": file_sha256(manifest_path), "manifest_id": manifest_id,
                       "completed_utc": datetime.now(timezone.utc).isoformat()}
            pointer_path = self.state / "latest.json"
            additional = sum(r["bytes"] for r in missing) + sum(r["bytes"] for r in mirror_uploads)
            if total + additional + manifest_path.stat().st_size + 4096 > self.policy["cloud_max_bytes"]:
                raise ResearchError("本轮写入将超过专属云端限额，未开始上传")
            self.charge(additional + 2 * manifest_path.stat().st_size + 4096)
            selected = self.state / "upload_files.txt"
            selected.write_text("".join(r["object"] + "\n" for r in missing))
            if missing:
                self.command("copy", self.state, self.remote, "--files-from", selected,
                             "--immutable", "--checksum", "--no-traverse", timeout=1800)
                checked, _ = self.inventory()
                for record in missing:
                    saved = checked.get(record["object"], {})
                    if saved.get("Size") != record["bytes"] or saved.get("Hashes", {}).get("md5") != record["md5"]:
                        raise ResearchError("上传后云端MD5/大小校验失败；未发布清单")
            self.command("copyto", manifest_path, self.remote + "/" + pointer["manifest"],
                         "--immutable", "--checksum")
            # Read back manifest bytes before making them discoverable.
            body = self.command("cat", self.remote + "/" + pointer["manifest"])
            if hashlib.sha256(body).hexdigest() != pointer["manifest_sha256"]:
                raise ResearchError("云端清单读回校验失败；未发布指针")
            for mirror in mirror_uploads:
                destination = self.remote + "/" + mirror["destination"]
                self.command("copyto", self.state / mirror["object"], destination, "--checksum")
                saved = json.loads(self.command("lsjson", destination, "--stat", "--hash"))
                if saved.get("Size") != mirror["bytes"] or saved.get("Hashes", {}).get("md5") != mirror["md5"]:
                    raise ResearchError("云端报告副本校验失败；未发布指针")
            pointer["completed_utc"] = datetime.now(timezone.utc).isoformat()
            atomic(pointer_path, pointer)
            self.command("copyto", pointer_path, self.remote + "/latest.json")
            status = {"status": "verified", **pointer, "file_count": len(records),
                      "uploaded_object_count": len(missing), "uploaded_bytes": additional,
                      "cloud_bytes_after": total + additional,
                      "report_mirrors": [{"path": r["destination"], "sha256": r["sha256"]} for r in mirrors],
                      "limits": {k: self.policy[k] for k in ("cloud_max_bytes", "cache_max_bytes", "transfer_max_bytes_per_day")}}
            atomic(self.state / "status.json", status)
            return status

    def _manifest(self, manifest_id=None):
        if manifest_id is None:
            self.charge(4096)
            pointer = json.loads(self.command("cat", self.remote + "/latest.json"))
            manifest_id = pointer["manifest_id"]
        else:
            pointer = None
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_id):
            raise ResearchError("云端清单ID无效")
        local = self.state / "manifests" / (manifest_id + ".json")
        if not local.exists():
            remote = self.remote + "/manifests/" + local.name
            info = json.loads(self.command("lsjson", remote, "--stat"))
            if not 0 < info["Size"] <= self.policy.get("manifest_max_bytes",16 * 1024**2):
                raise ResearchError("云端清单超过读取限额")
            self.charge(info["Size"])
            raw = self.command("cat", remote)
            if len(raw) != info["Size"]:
                raise ResearchError("云端清单大小在读取时发生改变")
            manifest = json.loads(raw)
            if digest(manifest) != manifest_id:
                raise ResearchError("云端清单内容指纹不匹配")
            atomic(local, manifest)
        manifest = json.loads(local.read_text())
        if digest(manifest) != manifest_id or (pointer and file_sha256(local) != pointer["manifest_sha256"]):
            raise ResearchError("归档清单指纹不匹配")
        return manifest_id, manifest

    def _evict(self, reserve=0, keep=None):
        if reserve > self.policy["cache_max_bytes"]:
            raise ResearchError("单个读取对象超过缓存上限")
        files = sorted((p for p in self.cache.iterdir() if p.is_file() and p != keep),
                       key=lambda p: p.stat().st_mtime_ns)
        used = directory_bytes([self.cache])
        while used + reserve > self.policy["cache_max_bytes"] and files:
            path = files.pop(0)
            size = path.stat().st_size
            path.unlink()  # Only disposable files inside this dedicated cache.
            used -= size
        if used + reserve > self.policy["cache_max_bytes"]:
            raise ResearchError("读取缓存无法在限额内容纳对象")
        if shutil.disk_usage(self.cache).free - reserve < self.policy["local_budget"]["min_free_bytes"]:
            raise ResearchError("本机空闲空间低于读取安全余量")

    def _get_object(self, row):
        if not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}", row["object"]) or row["object"].split("/")[-1] != row["sha256"]:
            raise ResearchError("云端对象路径/指纹无效")
        target = self.cache / row["sha256"]
        if target.exists():
            if target.stat().st_size != row["bytes"] or file_sha256(target) != row["sha256"]:
                raise ResearchError("读取缓存损坏，拒绝使用")
            os.utime(target, None)
            self._evict(keep=target)
            return target
        self._evict(reserve=row["bytes"])
        metadata = json.loads(self.command("lsjson", self.remote + "/" + row["object"], "--stat", "--hash"))
        if metadata.get("Size") != row["bytes"] or metadata.get("Hashes", {}).get("md5") != row["md5"]:
            raise ResearchError("云端读取对象大小/MD5与已发布清单不符")
        self.charge(row["bytes"])
        partial = target.with_suffix(".partial")
        self.command("copyto", self.remote + "/" + row["object"], partial,
                     "--max-transfer", str(row["bytes"]), "--cutoff-mode", "hard")
        if partial.stat().st_size != row["bytes"] or file_sha256(partial) != row["sha256"]:
            raise ResearchError("云端读取SHA256失败；不使用未验证数据")
        partial.replace(target)
        return target

    def get(self, logical_path, manifest_id=None):
        with self.locked():
            manifest_id, manifest = self._manifest(manifest_id)
            rows = [r for r in manifest["files"] if r["logical_path"] == logical_path]
            if len(rows) != 1:
                raise ResearchError("清单中没有唯一的目标行情/结果文件")
            row = rows[0]
            if "bundle" not in row:
                target = self._get_object(row)
            else:
                if row["member"] != row["sha256"] or not re.fullmatch(r"[0-9a-f]{64}", row["member"]):
                    raise ResearchError("归档包成员名称/指纹无效")
                target = self.cache / row["sha256"]
                if target.exists():
                    if target.stat().st_size != row["bytes"] or file_sha256(target) != row["sha256"]:
                        raise ResearchError("读取缓存损坏，拒绝使用")
                    os.utime(target, None)
                    self._evict(keep=target)
                    return target
                bundle = self._get_object(row["bundle"])
                self._evict(reserve=row["bytes"], keep=bundle)
                partial = target.with_suffix(".partial")
                with tarfile.open(bundle, mode="r:gz") as archive:
                    member = archive.getmember(row["member"])
                    if not member.isfile() or member.size != row["bytes"]:
                        raise ResearchError("归档包成员类型/大小无效")
                    with archive.extractfile(member) as source, partial.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                if partial.stat().st_size != row["bytes"] or file_sha256(partial) != row["sha256"]:
                    raise ResearchError("归档包成员SHA256失败，拒绝使用")
                partial.replace(target)
            atomic(self.state / "last_read.json", {"manifest_id": manifest_id, **row,
                                                   "verified_utc": datetime.now(timezone.utc).isoformat()})
            return target


def materialize_source(source):
    """Resolve a portable cloud reference for the existing parquet/CSV reader."""
    if "cloud" not in source:
        return source
    cloud = source["cloud"]
    path = CloudArchive(cloud["policy"]).get(cloud["logical_path"], cloud["manifest_id"])
    return {**source, "path": str(path), **({"compression": "gzip"} if cloud["logical_path"].endswith(".gz") else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["sync", "get", "status"])
    parser.add_argument("--policy", required=True)
    parser.add_argument("--logical-path")
    parser.add_argument("--manifest-id")
    args = parser.parse_args()
    archive = CloudArchive(args.policy)
    try:
        if args.action == "sync":
            result = archive.sync()
        elif args.action == "get":
            result = {"path": str(archive.get(args.logical_path, args.manifest_id))}
        else:
            path = archive.state / "status.json"
            result = json.loads(path.read_text()) if path.exists() else {"status": "not_synced"}
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        if isinstance(exc, ResearchError) and ("已有云端" in str(exc) or "本日" in str(exc)):
            print(json.dumps({"status": "deferred", "reason": str(exc)}, ensure_ascii=False))
            return
        atomic(archive.state / "last_failure.json", {"status": "failed", "error": str(exc),
               "utc": datetime.now(timezone.utc).isoformat()})
        raise


if __name__ == "__main__":
    main()
