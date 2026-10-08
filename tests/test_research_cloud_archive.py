import hashlib
import json
import shutil
from pathlib import Path

import pytest

from research.cloud_archive import CloudArchive
from research.config import ResearchError


class FakeDrive(CloudArchive):
    """Offline Drive semantics for quota, publication and corrupted-read checks."""
    def __init__(self, policy, remote):
        super().__init__(policy)
        self.files = remote
        self.operations = []
        self.fail_object = False
        self.corrupt_download = False

    def command(self, *args, **kwargs):
        self.operations.append(args)
        op = args[0]
        def relative(value):
            return str(value).removeprefix(self.remote + "/")
        def item(name, raw):
            return {"Path": name, "Size": len(raw), "Hashes": {"md5": hashlib.md5(raw).hexdigest()}}
        if op == "mkdir":
            return b""
        if op == "lsjson":
            if "--stat" in args:
                name = relative(args[1]); value = item(name, self.files[name])
            else:
                value = [item(n, v) for n, v in self.files.items()]
            return json.dumps(value).encode()
        if op == "cat":
            return self.files[relative(args[1])]
        if op == "copy":
            if self.fail_object:
                raise ResearchError("interrupted upload")
            selected = Path(args[args.index("--files-from") + 1]).read_text().splitlines()
            for name in selected:
                self.files[name] = (Path(args[1]) / name).read_bytes()
            return b""
        if op == "copyto":
            if str(args[1]).startswith(self.remote):
                raw = self.files[relative(args[1])]
                Path(args[2]).write_bytes(b"bad" if self.corrupt_download else raw)
            else:
                self.files[relative(args[2])] = Path(args[1]).read_bytes()
            return b""
        raise AssertionError(args)


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / "inputs"; source.mkdir()
    (source / "bars.parquet").write_bytes(b"verified real bytes placeholder")
    (source / "unfinished.partial").write_bytes(b"not publishable")
    policy = {"schema": 1, "remote": "gdrive:quant_backup/test-research",
              "local_state": str(tmp_path / "state"), "cache_root": str(tmp_path / "cache"),
              "cache_max_bytes": 100, "cloud_max_bytes": 1000000,
              "transfer_max_bytes_per_day": 1000000, "bandwidth": "4M",
              "local_budget": {"roots": [str(tmp_path)], "max_bytes": 1000000, "min_free_bytes": 0},
              "sources": [{"name": "inputs", "root": str(source)}]}
    path = tmp_path / "policy.json"; path.write_text(json.dumps(policy))
    return FakeDrive(path, {})


def test_sync_publishes_only_after_verified_objects_and_supports_fresh_cloud_read(archive):
    result = archive.sync()
    assert result["file_count"] == 1
    uploads = [x for x in archive.operations if x[0] in {"copy", "copyto"}]
    assert uploads[0][0] == "copy" and str(uploads[-1][2]).endswith("latest.json")
    path = archive.get("inputs/bars.parquet")
    assert path.read_bytes() == b"verified real bytes placeholder"
    manifest_id = result["manifest_id"]
    shutil.rmtree(archive.state / "manifests")
    assert archive.get("inputs/bars.parquet", manifest_id) == path
    second = archive.sync()
    assert second["manifest_id"] == manifest_id and second["uploaded_bytes"] == 0


def test_interrupted_upload_keeps_progress_and_does_not_publish_manifest(archive):
    archive.fail_object = True
    with pytest.raises(ResearchError, match="interrupted"):
        archive.sync()
    assert "latest.json" not in archive.files
    assert json.loads((archive.state / "transfer_ledger.json").read_text())
    archive.fail_object = False
    assert archive.sync()["status"] == "verified"


def test_dependency_lock_can_be_restored_while_runtime_locks_stay_local(archive):
    source = Path(archive.policy["sources"][0]["root"])
    dependencies = b"numpy==2.2.6\npandas==2.3.0\n"
    (source / "requirements.lock").write_bytes(dependencies)
    (source / "download.lock").write_bytes(b"temporary process lock")
    result = archive.sync()
    assert result["file_count"] == 2
    shutil.rmtree(archive.state / "manifests")
    assert archive.get("inputs/requirements.lock", result["manifest_id"]).read_bytes() == dependencies
    with pytest.raises(ResearchError, match="没有唯一"):
        archive.get("inputs/download.lock", result["manifest_id"])
    assert (source / "download.lock").exists()


def test_cloud_corruption_and_read_corruption_are_rejected(archive):
    result = archive.sync()
    name = next(n for n in archive.files if n.startswith("objects/"))
    archive.files[name] = b"changed"
    with pytest.raises(ResearchError, match="不可变对象"):
        archive.sync()
    with pytest.raises(ResearchError, match="大小/MD5"):
        archive.get("inputs/bars.parquet", result["manifest_id"])
    archive.files[name] = b"verified real bytes placeholder"
    archive.corrupt_download = True
    with pytest.raises(ResearchError, match="SHA256"):
        archive.get("inputs/bars.parquet", result["manifest_id"])
    assert not (archive.cache / name.split("/")[-1]).exists()


def test_transfer_cloud_and_cache_limits_stop_before_exceeding_policy(archive):
    archive.policy["transfer_max_bytes_per_day"] = 10
    with pytest.raises(ResearchError, match="本日"):
        archive.sync()
    assert not archive.files
    archive.policy["transfer_max_bytes_per_day"] = 1000000
    archive.policy["cloud_max_bytes"] = 10
    with pytest.raises(ResearchError, match="云端限额"):
        archive.sync()
    assert not archive.files
    archive.policy["cloud_max_bytes"] = 1000000
    archive.sync()
    archive.policy["cache_max_bytes"] = 10
    with pytest.raises(ResearchError, match="缓存上限"):
        archive.get("inputs/bars.parquet")


def test_cache_evicts_its_oldest_files_and_retains_verified_recent_file(archive):
    archive.sync()
    old = archive.cache / ("a" * 64); old.write_bytes(b"x" * 80)
    recent = archive.get("inputs/bars.parquet")
    assert not old.exists() and recent.exists()
    recent.write_bytes(b"bad")
    with pytest.raises(ResearchError, match="缓存损坏"):
        archive.get("inputs/bars.parquet")


def test_backup_refuses_credentials_and_preserves_other_roots(archive):
    credential = Path(archive.policy["sources"][0]["root"]) / "rclone.conf"
    credential.write_text("synthetic credential test")
    with pytest.raises(ResearchError, match="凭据"):
        archive.sync()
    assert credential.exists()


def test_packed_backup_is_repeatable_and_individual_files_read_back_with_sha256(archive):
    archive.policy["pack_objects"] = True
    archive.policy["cache_max_bytes"] = 100000
    first = archive.sync()
    manifest = json.loads((archive.state / first["manifest"]).read_text())
    assert "bundle" in manifest["files"][0]
    row = manifest["files"][0]
    assert row["bundle"]["object"] in archive.files and row["object"] not in archive.files
    assert archive.get("inputs/bars.parquet", first["manifest_id"]).read_bytes() == b"verified real bytes placeholder"
    second = archive.sync()
    assert second["manifest_id"] == first["manifest_id"] and second["uploaded_bytes"] == 0
    for path in archive.cache.iterdir(): path.unlink()
    archive.files[row["bundle"]["object"]] = b"corrupt bundle"
    with pytest.raises(ResearchError, match="大小/MD5"):
        archive.get("inputs/bars.parquet", first["manifest_id"])


def test_readable_report_mirror_is_checked_reused_and_keeps_immutable_archive(archive):
    source = Path(archive.policy["sources"][0]["root"]) / "review.html"
    source.write_text("<html>verified review</html>")
    archive.policy["report_mirrors"] = {"inputs/review.html": "reports/2026-10-05/review.html"}
    first = archive.sync()
    assert archive.files["reports/2026-10-05/review.html"] == source.read_bytes()
    assert first["report_mirrors"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    archive.operations.clear()
    archive.sync()
    assert not any(op[0] == "copyto" and str(op[2]).endswith("review.html") for op in archive.operations)
    archive.policy["report_mirrors"] = {"inputs/review.html": "../other/review.html"}
    with pytest.raises(ResearchError, match="专属reports"):
        archive.sync()


def test_corrupt_report_mirror_prevents_publication(archive, monkeypatch):
    source = Path(archive.policy["sources"][0]["root"]) / "review.html"
    source.write_text("<html>review</html>")
    archive.policy["report_mirrors"] = {"inputs/review.html": "reports/review.html"}
    command = archive.command
    def damaged(*args, **kwargs):
        result = command(*args, **kwargs)
        if args[0] == "copyto" and str(args[2]).endswith("reports/review.html"):
            archive.files["reports/review.html"] = b"bad"
        return result
    monkeypatch.setattr(archive, "command", damaged)
    with pytest.raises(ResearchError, match="报告副本校验失败"):
        archive.sync()
    assert "latest.json" not in archive.files


def test_cloud_command_uses_dedicated_config_and_same_proxy_as_automatic_service(archive, monkeypatch):
    from types import SimpleNamespace
    import os
    archive.policy.update(rclone_config="/private/quant-googledrive.conf", proxy="http://127.0.0.1:10808")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:20171")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"[]", stderr=b"")
    monkeypatch.setattr("research.cloud_archive.subprocess.run", run)
    assert CloudArchive.command(archive, "lsjson", archive.remote) == b"[]"
    command, arguments = calls[0]
    assert command[command.index("--config") + 1] == "/private/quant-googledrive.conf"
    assert arguments["env"]["HTTPS_PROXY"] == "http://127.0.0.1:10808"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:20171"
