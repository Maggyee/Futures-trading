"""Dated execution documents and monthly retrospective diagnostic evaluations."""

import copy
import fcntl
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .acquisition import DownloadAllowance, atomic_json, monthly_calendar, month_bounds
from .calendar import Calendar, MINUTE, at, stamp
from .config import ResearchError, digest, read_config, validate_config
from .cost_review import PARAMETERS, parameter_rows
from .coverage_expansion import ORIGINAL, declaration, save_summary, storage_config
from .data import file_sha256, load_data
from .exchange_diagnostic import cffex_parameters, czce_parameters, gfex_parameters
from .execution_parameters import ExecutionParameters
from .experiments import run_one
from .reporting import write_json
from .storage import SpaceBudget


class Documents:
    def __init__(self, root, budget, allowance):
        self.root, self.budget = Path(root), budget
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowance = DownloadAllowance(allowance)
        self.lock_stream = (Path(allowance["ledger"]).parent / "acquire.lock").open("a")
        try:
            fcntl.flock(self.lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_stream.close()
            raise ResearchError("已有行情下载进行中；官方资料等待下一轮，保持串行") from None
        self.http = httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=True,
                                 headers={"User-Agent":"Mozilla/5.0 QuantResearch/1.0"})

    def close(self):
        self.http.close()
        self.lock_stream.close()

    def fetch(self, name, url, form=None):
        host = urlsplit(url).hostname
        if host not in {"www.shfe.com.cn", "www.czce.com.cn", "www.cffex.com.cn", "www.gfex.com.cn"} or Path(name).name != name:
            raise ResearchError("历史执行资料来源/文件名无效")
        path = self.root / name
        receipt = path.with_suffix(path.suffix + ".receipt.json")
        identity = {"url": url, "method": "POST" if form else "GET", "form": form}
        if path.exists():
            if not receipt.exists():
                raise ResearchError("历史官方资料缺少校验凭据，不覆盖")
            saved = json.loads(receipt.read_text())
            if any(saved.get(k) != v for k,v in identity.items()) or saved["sha256"] != file_sha256(path):
                raise ResearchError("历史官方资料来源/指纹不同，不覆盖")
        else:
            self.allowance.consume(requests=1)
            partial = path.with_suffix(path.suffix + ".partial")
            self.budget.check(partial, reserve=2 * 1024**2)
            with self.http.stream(identity["method"], url, data=form) as response:
                if response.status_code != 200 or urlsplit(str(response.url)).hostname != host:
                    raise ResearchError(f"历史官方资料HTTP状态/主机无效：{response.status_code}")
                size = 0
                with partial.open("wb") as output:
                    for block in response.iter_bytes(65536):
                        size += len(block)
                        self.allowance.consume(len(block))
                        if size > 2 * 1024**2:
                            raise ResearchError("历史官方资料超过2MiB限额")
                        self.budget.check(partial, reserve=len(block))
                        output.write(block)
                partial.replace(path)
            saved = {**identity, "sha256": file_sha256(path), "bytes": size,
                     "requested_utc": datetime.now(timezone.utc).isoformat(), "credential_used": False}
            atomic_json(receipt, saved, self.budget)
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
        return text, {"path": str(path.resolve()), **saved}


def cffex_schedules(documents, days, calendar):
    """CFFEX publishes dated schedules on changes, not one CSV each day."""
    text,index_source = documents.fetch("cffex_schedule_index.html", "http://www.cffex.com.cn/cn/jscs.html")
    codes = sorted(set(re.findall(r'/sj/jscs/\d{6}/\d{2}/(\d{8})_1\.csv', text)))
    dated = [(code[:4]+"-"+code[4:6]+"-"+code[6:], code) for code in codes]
    earliest, latest = calendar.previous(days[0]), calendar.previous(days[-1])
    predecessors = [item for item in dated if item[0] <= earliest]
    needed = ([max(predecessors)] if predecessors else []) + [item for item in dated if earliest < item[0] <= latest]
    schedules = []
    for day,code in needed:
        body,source = documents.fetch("cffex_schedule_"+code+".csv", f"http://www.cffex.com.cn/sj/jscs/{code[:6]}/{code[6:]}/{code}_1.csv")
        schedules.append((day, cffex_parameters(body, day), source))
    return schedules, index_source


def document_batch(plan_path, month):
    path, plan, _ = declaration(plan_path)
    if month not in plan["months"]:
        raise ResearchError("月份未预先声明")
    root = path.parent / month
    settings = json.loads((root / "acquire.json").read_text())
    cal = monthly_calendar(month, json.loads((root / "raw/holidays.json").read_text()), 5)
    calendar = Calendar(cal)
    start, end = month_bounds(month)
    days = [d for d in calendar.days if start.isoformat() <= d < end.isoformat()]
    documents = Documents(root / "official", SpaceBudget(plan["budget"]), settings["download_allowance"])
    errors, complete = [], []
    try:
        cffex_schedules(documents, days, calendar)
        for day in days:
            previous = calendar.previous(day)
            code = previous.replace("-", "")
            jobs = [(table + code + ".json", url.format(day=code), None) for table,url in PARAMETERS.items()]
            jobs += [
                ("czce_" + code + ".txt", f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{previous[:4]}/{code}/FutureDataClearParams.txt", None),
                ("gfex_lc_" + code + ".json", "http://www.gfex.com.cn/u/interfacesWebTiFutAndOptSettle/loadList", {"trade_date": code, "variety": "lc"}),
            ]
            for name,url,form in jobs:
                try:
                    documents.fetch(name, url, form)
                    complete.append(name)
                except (ResearchError, httpx.HTTPError, ValueError) as exc:
                    if "预算" in str(exc) or "余量" in str(exc):
                        raise
                    errors.append({"name": name, "date": previous, "reason": str(exc)})
            atomic_json(root / "official_progress.json", {"month": month, "completed": complete, "errors": errors}, documents.budget)
            print(json.dumps({"phase": "official_documents", "month": month, "date": previous,
                              "completed": len(complete), "errors": len(errors)}, ensure_ascii=False), flush=True)
    finally:
        documents.close()
    return {"completed": complete, "errors": errors}


def prepare(plan_path, month, unused_k=2):
    path, plan, parent = declaration(plan_path)
    if month not in plan["months"]:
        raise ResearchError("月份未预先声明")
    root, output = path.parent / month, Path(plan["budget"]["roots"][1]) / month
    acquisition = json.loads((root / "progress.json").read_text())
    if acquisition["status"] not in {"data_download_completed", "completed_with_gaps"}:
        raise ResearchError("先完成该月份行情下载及覆盖诊断")
    acquired = read_config(root / "config.json")
    cfg = copy.deepcopy(parent)
    cfg["metadata"] = acquired["metadata"]
    cfg["calendar"] = copy.deepcopy(acquired["calendar"])
    # Retain the original split lock's exact calendar; no September test bars are loaded.
    cfg["calendar"]["trading_days"] = sorted(set(cfg["calendar"]["trading_days"] + parent["calendar"]["trading_days"]))
    cfg["calendar"]["profiles"].update(parent["calendar"]["profiles"])
    cfg["data"] = acquired["data"]
    storage_config(cfg, plan, month)
    output.mkdir(parents=True, exist_ok=True)
    budget = SpaceBudget(plan["budget"])
    start, end = month_bounds(month)
    calendar = Calendar(cfg["calendar"])
    days = [d for d in calendar.days if start.isoformat() <= d < end.isoformat()]
    window = {"start": days[0], "end": days[-1]}
    train_ref = json.loads((ORIGINAL / "data_reference.json").read_text())
    declaration_record = {"sample_status": "retrospective_after_calibration", "locked_test_read": False,
        "parent_config": str(Path(plan["parent_run"]) / "config_snapshot.json"), "parent_config_sha256": plan["parent_config_sha256"],
        "plan": str(path), "plan_sha256": file_sha256(path), "window": window, "candidate_k": plan["candidate_k"],
        "training_reference_run": str(ORIGINAL), "training_reference_sha256": train_ref["sha256"]}
    cfg["archive_evaluation"] = declaration_record
    q = copy.deepcopy(parent["execution"]["qualification"])
    q.update(schema=3, validation_window=window, archive_evaluation=declaration_record, rules=[], rule_rejections={}, sources=[],
             supplier_specification_continuity_assumed=True, allowed_exchanges=["SHFE", "INE", "CZCE", "CFFEX", "GFEX"])
    q["assumptions"] += [
        "本轮7、8月是冻结9月参数后的历史回放；不是严格样本外或独立验证，绝不重新拟合跳数、斜率或止盈。",
        "新增合约没有有日期官方规格时，明确假设供应商跳价及乘数历史延续；费率必须来自对应前一交易日官方资料。",
        "缺少DCE执行资料及GFEX非LC历史限额时拒绝执行；排名池保留，不用低位品种替补。",
        "新增CZCE/CFFEX上市日期沿用窗口首个真实日线代理；未声称完整历史上市目录已核实。",
    ]
    selections = json.loads((root / "daily_selection.json").read_text())
    meta_by_key = {r["symbol"] + "." + r["exchange"]: r for r in cfg["metadata"]["contracts"]}
    exchange_by_product = {r["product"]:r["exchange"] for r in meta_by_key.values()}
    old_specs = {''.join(x for x in r["contract"].split(".")[0] if x.isalpha()): r for r in parent["execution"]["qualification"]["rules"]}
    docs = Documents(root / "official", budget, json.loads((root / "acquire.json").read_text())["download_allowance"])
    source_errors = []
    try:
        schedules,index_source = cffex_schedules(docs, days, calendar)
        q["sources"].append(index_source)
        for day in days:
            previous, parsed, sources = calendar.previous(day), {}, []
            code = previous.replace("-", "")
            available_shfe = at(previous, "23:59:59")
            try:
                bodies = {}
                for table,url in PARAMETERS.items():
                    text, src = docs.fetch(table + code + ".json", url.format(day=code))
                    bodies[table] = json.loads(text); sources.append(src)
                converted = parameter_rows(bodies["ContractBaseInfo"], bodies["Settlement"], exchange_by_product)
                available_shfe = max(stamp(datetime.strptime(x["update_date"], "%Y%m%d %H:%M:%S")) for x in bodies.values())
                if available_shfe.date().isoformat() != previous or available_shfe < at(previous, "15:00"):
                    raise ResearchError("官方SHFE参数发布时间错配")
                for row in converted:
                    if row["contract"] and not row["issues"]:
                        parsed[row["contract"]] = {"margin_rate": row["exchange_speculation_margin"]["long"],
                            "fees": row["exchange_fee_reference"], "listed": row["listed"], "expiry": row["last_trade_date"], "sources": sources[:], "available_at": available_shfe}
            except (ResearchError, httpx.HTTPError, ValueError, KeyError) as exc:
                source_errors.append({"day": day, "exchange": "SHFE/INE", "error": str(exc)})
            for exchange in ("CZCE", "CFFEX", "GFEX"):
                try:
                    if exchange == "CZCE":
                        text, src = docs.fetch("czce_" + code + ".txt", f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{previous[:4]}/{code}/FutureDataClearParams.txt")
                        rows = czce_parameters(text, previous)
                    elif exchange == "CFFEX":
                        source_day, rows, src = max((item for item in schedules if item[0] <= previous), key=lambda item:item[0])
                    else:
                        text, src = docs.fetch("gfex_lc_" + code + ".json", "http://www.gfex.com.cn/u/interfacesWebTiFutAndOptSettle/loadList", {"trade_date": code, "variety": "lc"})
                        rows = gfex_parameters(json.loads(text), previous)
                    for symbol,row in rows.items():
                        reference_day = source_day if exchange == "CFFEX" else previous
                        parsed[symbol + "." + exchange] = {**row, "sources": [src], "available_at": at(reference_day, "23:59:59"),
                            "source_date": reference_day, "source_basis": "dated_schedule_continuity" if exchange == "CFFEX" else "previous_close_archive"}
                    sources.append(src)
                except (ResearchError, httpx.HTTPError, ValueError, KeyError) as exc:
                    source_errors.append({"day": day, "exchange": exchange, "error": str(exc)})
            q["sources"].extend(sources)
            for pick in [r for r in selections if r["date"] == day]:
                key, product = pick["contract"], pick["product"]
                base, row = meta_by_key[key], parsed.get(key)
                reasons = []
                if product not in q["training_ready_products"]:
                    reasons.append("training_product_not_qualified")
                if not row:
                    reasons.append("exact_previous_parameter_missing")
                if reasons:
                    q["rule_rejections"][day + "/" + key] = reasons
                    continue
                opening, close = calendar.bounds(day, base)
                spec = old_specs.get(product)
                specification = {"specification_basis": "supplier_specification_continuity_assumed"}
                if spec and spec.get("specification_available_at") and stamp(spec["specification_available_at"]) < opening:
                    specification = {k: spec[k] for k in ("specification_basis", "specification_available_at", "specification_effective_from") if k in spec}
                rule = {"contract": key, "trading_day": day, "source_date": row.get("source_date", previous),
                    "source_basis": row.get("source_basis", "previous_close_archive"), "available_at": row["available_at"].isoformat(),
                    "margin_effective_at": at(row.get("source_date",previous), "15:00").isoformat(), "effective_from": opening.isoformat(),
                    "effective_to": (close + MINUTE).isoformat(), "tick_size": base["tick_size"],
                    "value_per_price": base["value_per_price"], "listed": row.get("listed", base["listed"]),
                    "expiry": row.get("expiry", base["expiry"]), "margin_rate": row["margin_rate"],
                    "fees": row["fees"], "sources": row["sources"], **specification}
                if row.get("daily_open_limit"):
                    rule["daily_open_limit"] = row["daily_open_limit"]
                if base["exchange"] == "GFEX":
                    rule.update(min_open_lots=1, daily_open_limit=10000, historical_limit_continuity_assumed=True)
                if not rule["listed"] <= day <= rule["expiry"]:
                    q["rule_rejections"][day + "/" + key] = ["official_lifecycle_not_active"]
                    continue
                q["rules"].append(rule)
    finally:
        docs.close()
    q["sources"] = list({r["path"]: r for r in q["sources"]}.values())
    cfg["execution"].update(qualification=q, qualification_hash=digest(q))
    cfg["storage"]["stream_signals"] = True
    cfg["storage"]["record_unselected_signals"] = False
    cfg["development_review"] = {"step": "frozen_historical_coverage", "sample_status": "retrospective_after_calibration", "parameter_search": False, "locked_test_read": False}
    validate_config(cfg)
    from .data import Metadata
    ExecutionParameters(cfg, Metadata(cfg["metadata"]))
    write_json(output / "historical_config.json", cfg, budget)
    summary = {"month": month, "window": window, "rules": len(q["rules"]), "execution_products": sorted({meta_by_key[r["contract"]]["product"] for r in q["rules"]}),
               "rule_rejections": len(q["rule_rejections"]), "source_errors": source_errors, "locked_test_read": False}
    write_json(output / "execution_coverage.json", summary, budget)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def run(plan_path, month, k):
    path, plan, _ = declaration(plan_path)
    if month not in plan["months"] or k not in plan["candidate_k"]:
        raise ResearchError("月份/K不在预先声明的对照内")
    output = Path(plan["budget"]["roots"][1]) / month
    cfg = read_config(output / "historical_config.json")
    cfg["strategy"]["k"] = k
    cfg["baseline_expectation"]["strategy"]["k"] = k
    window = cfg["archive_evaluation"]["window"]
    data = load_data(cfg, cutoff=window["end"])
    print(json.dumps({"phase": "historical_data_ready", "month": month, "k": k, "bars": len(data.bars), "errors": len(data.quality["errors"])}), flush=True)
    directory, result = run_one(data, cfg, output / ("k" + str(k)), window, split="retrospective")
    summary = save_summary(plan, month, k, directory, result)
    if hasattr(result["signals"], "discard"):
        result["signals"].discard()
    return summary
