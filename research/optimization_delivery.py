"""Check final report bytes and replay evidence against the published cloud copy."""

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .cloud_archive import CloudArchive
from .config import ResearchError
from .data import file_sha256
from .optimization_review import PLAN
from .reporting import write_json
from .storage import SpaceBudget, directory_bytes


def verify(plan_path=PLAN):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text())
    output = Path(plan["output"])
    root = output.parent
    policy = plan_path.parent/"cloud_policy.json"
    archive = CloudArchive(policy)
    report = root/"optimization_review.html"
    data = json.loads((root/"optimization_report_data.json").read_text())
    browser = json.loads((root/"optimization_browser_verification.json").read_text())
    if browser.get("status") != "passed" or browser.get("report_sha256") != file_sha256(report):
        raise ResearchError("交付的HTML与已通过浏览器验证的报告版本不同")
    critical = {plan_path, Path(plan["coverage_plan"]), policy, report,
                root/"optimization_report_data.json", output/"assessment.json", output/"final_assessment.json",
                root/"optimization_browser_verification.json", plan_path.parent/"reproducibility_latest.json"}
    snapshot = json.loads((plan_path.parent/"reproducibility_latest.json").read_text())
    bundle = Path(snapshot["directory"])
    contents = json.loads((bundle/"manifest.json").read_text())
    critical.add(bundle/"manifest.json")
    critical.update(bundle/r["relative"] for r in contents["files"])
    for scenario in data["scenarios"]:
        run = Path(scenario["directory"])
        names = ["manifest.json", "config_snapshot.json", "summary.json", "trades.csv.gz", "signals.csv.gz",
                 "orders.csv.gz", "events.csv.gz", "equity.csv.gz", "daily_pool.csv.gz", "daily_candidates.csv.gz",
                 "candidate_execution.csv.gz", "independent_coverage_audit.json", "source_snapshot.tar.gz",
                 "data_reference.json", "prepared_source_review.json"]
        if (run/"prepared_entries_review.json").exists():
            names.append("prepared_entries_review.json")
        if (run/"source_archive_recovery.json").exists():
            names.append("source_archive_recovery.json")
        critical.update(run/name for name in names)
        critical.add(output/(scenario["month"]+"_"+scenario["variant"]+"_latest.json"))
        reference = json.loads((run/"data_reference.json").read_text())
        source = (run/reference["object"]).resolve()
        if file_sha256(source) != reference["sha256"]:
            raise ResearchError("交付前原行情对象发生改变")
        critical.add(source)
        evidence = json.loads((run/"prepared_source_review.json").read_text())
        critical.add(Path(evidence["cache"]))
    sources = sorted(archive.policy["sources"],key=lambda source:len(Path(source["root"]).parts),reverse=True)
    def logical(path):
        path = path.resolve()
        for source in sources:
            base = Path(source["root"]).resolve()
            if path.is_relative_to(base):
                return source["name"]+"/"+path.relative_to(base).as_posix()
        raise ResearchError("交付证据没有已声明的云端映射："+str(path))
    verified = []
    with archive.locked():
        manifest_id, manifest = archive._manifest()
        files = {r["logical_path"]:r for r in manifest["files"]}
        for path in sorted(critical):
            key = logical(path)
            saved = files.get(key)
            if saved is None or saved["sha256"] != file_sha256(path) or saved["bytes"] != path.stat().st_size:
                raise ResearchError("最新云端快照缺少相同的交付文件："+key)
            verified.append({"logical_path":key,"sha256":saved["sha256"]})
        archive.charge(report.stat().st_size)
        remote_report = archive.remote+"/reports/2026-10-05/optimization_review.html"
        body = archive.command("cat", remote_report)
        if body != report.read_bytes():
            raise ResearchError("新HTML云端报告真实读回与本地不同")
    # Also exercise the normal bounded archive reader, using the exact published
    # manifest rather than a mounting cache or a local source fallback.
    read_path = archive.get(logical(report),manifest_id)
    if file_sha256(read_path) != file_sha256(report):
        raise ResearchError("按需报告读取SHA256不符")
    services = {}
    for name in ("rclone-googledrive.service", "quant-research-acquire.timer", "quant-research-archive.timer"):
        services[name] = {field: subprocess.run(["systemctl",cmd,name],capture_output=True,text=True).stdout.strip()
                          for field,cmd in (("enabled","is-enabled"),("active","is-active"))}
        if services[name] != {"enabled":"enabled","active":"active"}:
            raise ResearchError("交付要求原挂载与研究同步定时服务保持启用："+name)
    result = {"status":"passed", "verified_utc":datetime.now(timezone.utc).isoformat(),
              "manifest_id":manifest_id, "report_sha256":hashlib.sha256(body).hexdigest(),
              "cloud_report_path":remote_report, "cloud_report_readback_equal":True,
              "bounded_reader_sha256_equal":True, "critical_file_hashes_verified":len(verified),
              "complete_runs_backed_up":len(data["scenarios"]), "files":verified,
              "reproducibility_snapshot":snapshot["snapshot_id"], "services":services,
              "local_bytes":directory_bytes(plan["budget"]["roots"]), "cache_bytes":directory_bytes([archive.cache]),
              "transfer_reserved_bytes_by_day":json.loads((archive.state/"transfer_ledger.json").read_text()),
              "locked_test_read":False, "live_trading_changed":False}
    write_json(root/"optimization_delivery_verification.json",result,SpaceBudget(plan["budget"]))
    print(json.dumps({key:result[key] for key in ("status","manifest_id","report_sha256","critical_file_hashes_verified","complete_runs_backed_up","cloud_report_readback_equal")},ensure_ascii=False))
    return result


if __name__ == "__main__":
    verify()
