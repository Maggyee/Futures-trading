"""Execution qualifications live outside pool selection and shared signal logic."""

import copy
import math
import json
from collections import defaultdict
from pathlib import Path

from .calendar import Calendar, stamp
from .config import ResearchError, digest, execution_gaps


def validate_archive_reference(cfg, qualification):
    """A September freeze may be inspected on earlier data, never called OOS."""
    from .data import file_sha256
    declaration = qualification.get("archive_evaluation", {})
    if declaration != cfg.get("archive_evaluation") or declaration.get("sample_status") != "retrospective_after_calibration" or declaration.get("locked_test_read") is not False:
        raise ResearchError("历史回放必须明确声明校准后回看及保留测试锁")
    reference = Path(declaration["parent_config"])
    if file_sha256(reference) != declaration["parent_config_sha256"]:
        raise ResearchError("历史回放的冻结策略来源已改变")
    parent = json.loads(reference.read_text())
    if cfg["calibration_snapshot"] != parent["calibration_snapshot"] or cfg["risk"] != parent["risk"] or cfg["splits"] != parent["splits"]:
        raise ResearchError("历史扩展必须保持原校准、资金规则及时间锁")
    if cfg.get("optimization_review"):
        from .optimization_declaration import validate_optimization
        validate_optimization(cfg)
    elif {k:v for k,v in cfg["strategy"].items() if k != "k"} != {k:v for k,v in parent["strategy"].items() if k != "k"} or cfg["strategy"]["k"] not in declaration["candidate_k"]:
        raise ResearchError("历史扩展只允许预先声明的K对照，不重新拟合参数")
    if "metadata" in parent:
        units = defaultdict(set)
        for row in parent["metadata"]["contracts"]:
            units[row["product"]].add((row.get("tick_size"), row.get("value_per_price")))
        for row in cfg["metadata"]["contracts"]:
            if row["product"] in parent["calibration_snapshot"]["products"] and (row.get("tick_size"), row.get("value_per_price")) not in units[row["product"]]:
                raise ResearchError("历史合约单位与原跳数校准不一致，不能沿用冻结参数")
    plan = Path(declaration["plan"])
    if file_sha256(plan) != declaration["plan_sha256"]:
        raise ResearchError("历史扩展声明发生改变")
    prepared = json.loads(plan.read_text())
    window = declaration["window"]
    if declaration["candidate_k"] != prepared["candidate_k"] or window["start"][:7] not in prepared["months"] or window["start"][:7] != window["end"][:7] or not window["start"] <= window["end"] < parent["splits"]["train"]["start"] or qualification["validation_window"] != window:
        raise ResearchError("历史回放超出预先声明月份或触及原训练/测试")
    source_run = Path(declaration["training_reference_run"])
    source_cfg = json.loads((source_run / "config_snapshot.json").read_text())
    source_ref = json.loads((source_run / "data_reference.json").read_text())
    source_data = (source_run / source_ref["object"]).resolve()
    if source_cfg["calibration_snapshot"] != cfg["calibration_snapshot"] or file_sha256(source_data) != source_ref["sha256"] or source_ref["sha256"] != declaration["training_reference_sha256"]:
        raise ResearchError("历史回放的原训练数据与校准冻结来源不符")
    return window


def execution_mode(cfg):
    mode = cfg.get("execution", {}).get("mode", "formal")
    if mode not in {"formal", "diagnostic"}:
        raise ResearchError("execution.mode 必须为 formal 或 diagnostic")
    return mode


class ExecutionParameters:
    """Resolve reviewed parameters at a decision/fill time, without changing metadata.

    Formal mode continues to use the original metadata and strict global checks.
    Diagnostic mode requires a training freeze and exact-session rules. Missing
    rules are rejections, never a fallback to another contract or an older date.
    """

    def __init__(self, cfg, metadata):
        self.cfg, self.metadata = cfg, metadata
        self.mode = execution_mode(cfg)
        self.records = defaultdict(list)
        self.qualification = cfg.get("execution", {}).get("qualification", {})
        if self.mode == "formal":
            return
        q = self.qualification
        if (
            q.get("schema") not in {1, 2, 3}
            or q.get("kind") != "DIAGNOSTIC_EXECUTION_ONLY"
        ):
            raise ResearchError("诊断回测缺少独立执行资格文件")
        if cfg["execution"].get("qualification_hash") != digest(q):
            raise ResearchError("诊断执行资格指纹不匹配")
        self.archive_window = validate_archive_reference(cfg, q) if q["schema"] == 3 else None
        calibration = cfg.get("calibration_snapshot", {})
        if (
            q.get("training_window") != cfg["splits"]["train"]
            or calibration.get("train_window") != q["training_window"]
            or q.get("calibration_hash") != digest(calibration)
            or q.get("locked_test_read") is not False
            or calibration.get("locked_test_read") is not False
            or calibration.get("schema") != 2
            or (self.archive_window is None and q.get("validation_window") != cfg["splits"]["validation"])
            or cfg["splits"]["validation"]["end"] >= cfg["splits"]["test"]["start"]
        ):
            raise ResearchError("诊断资格必须来自同一训练截止日，且不读取锁定测试")
        if set(q["training_ready_products"]) != set(calibration["products"]):
            raise ResearchError("不能给训练不足品种补造执行资格")
        if q.get("fixed_ticks_hash") != digest(cfg["strategy"]["fixed_ticks"]):
            raise ResearchError("固定保护参数与诊断冻结版本不一致")
        if not q.get("assumptions") or q.get("fee_model") != "exchange_only":
            raise ResearchError("诊断必须声明费用口径及未确认假设")
        calendar = Calendar(cfg["calendar"])
        for row in q["rules"]:
            day, key = row["trading_day"], row["contract"]
            start, end = stamp(row["effective_from"]), stamp(row["effective_to"])
            if (
                not q["validation_window"]["start"]
                <= day
                <= q["validation_window"]["end"]
            ):
                raise ResearchError("诊断规则超出预先声明的验证窗口")
            basis = row.get("source_basis", "previous_close_archive")
            previous = calendar.previous(day)
            valid_source = row["source_date"] == previous
            if q["schema"] in {2, 3} and basis == "dated_schedule_continuity":
                valid_source = row["source_date"] <= previous
            elif basis != "previous_close_archive":
                raise ResearchError("未知执行规则时间口径")
            if (
                not valid_source
                or stamp(row["available_at"]) > start
                or stamp(row["margin_effective_at"]) > start
                or start >= end
                or start.date().isoformat() != day
                or end.date().isoformat() != day
            ):
                raise ResearchError("执行参数不能使用当日收盘资料或错配前日生效时间")
            for field in ("specification_available_at", "specification_effective_from"):
                if row.get(field) and stamp(row[field]) > start:
                    raise ResearchError(
                        "合约规范在交易时刻尚未公开或生效；不以训练截止代替交易时刻"
                    )
            if row.get("specification_basis") == "current_official_continuity_assumed":
                if q["schema"] not in {2, 3} or not q.get(
                    "current_specification_continuity_assumed"
                ):
                    raise ResearchError("当前规范不能静默当作已核实历史规范")
            if (
                row.get("specification_basis")
                == "supplier_specification_continuity_assumed"
            ):
                if q["schema"] not in {2, 3} or not q.get(
                    "supplier_specification_continuity_assumed"
                ):
                    raise ResearchError(
                        "供应商规格必须明确声明历史延续假设，不能标为正式核实"
                    )
            for field in ("min_open_lots", "daily_open_limit"):
                value = row.get(field)
                if value is not None and (type(value) is not int or value < 1):
                    raise ResearchError(f"执行手数约束无效：{key}/{field}")
            for field in ("tick_size", "value_per_price", "margin_rate"):
                value = row.get(field)
                if (
                    not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ResearchError(f"诊断执行参数无效：{key}/{field}")
            if row["margin_rate"] > 1:
                raise ResearchError("诊断保证金比例超过1")
            if not row["listed"] <= day <= row["expiry"]:
                raise ResearchError("诊断规则合约不在官方上市/最后交易日期内")
            fees = row.get("fees", {})
            for side in ("open", "close_today", "close_yesterday"):
                item = fees.get(side, {})
                value = item.get("value")
                if (
                    item.get("mode") not in {"fixed", "rate"}
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ResearchError(f"诊断费用规则无效：{key}/{side}")
            base = metadata.get(key, day)
            if not base or any(
                base.get(f) != row[f] for f in ("tick_size", "value_per_price")
            ):
                raise ResearchError(
                    "官方跳动/合约价值与校准及信号元数据不一致，须重新校准"
                )
            self.records[(day, key)].append(row)
        for rows in self.records.values():
            rows.sort(key=lambda r: r["effective_from"])
            if any(
                a["effective_to"] > b["effective_from"]
                for a, b in zip(rows, rows[1:], strict=False)
            ):
                raise ResearchError("诊断执行时段规则重叠")

    def preflight(self, products, start, end, data=None):
        if self.mode == "formal":
            gaps = execution_gaps(self.cfg, products)
            if gaps:
                raise ResearchError("正式回测配置缺失：\n" + "\n".join(gaps))
        elif {"start": start, "end": end} != self.qualification["validation_window"]:
            raise ResearchError("诊断只允许冻结的验证窗口；不能进入锁定测试或改动边界")
        elif (
            self.archive_window is None
            and
            data is not None
            and data.training_fingerprint(self.qualification["training_window"]["end"])
            != self.cfg["calibration_snapshot"]["train_data_hash"]
        ):
            raise ResearchError("诊断准备后训练数据已改变，拒绝沿用旧执行资格和跳数")

    def resolve(self, key, time):
        time = stamp(time)
        day = time.date().isoformat()
        base = self.metadata.get(key, day)
        if self.mode == "formal":
            return base, []
        q = self.qualification
        reasons = []
        product = base["product"] if base else None
        if product not in q["training_ready_products"]:
            missing = q.get("training_missing_products", {}).get(product, {})
            reasons.extend(
                ["training_insufficient:" + r for r in missing.get("reasons", [])]
            )
            if not reasons:
                reasons.append("training_product_not_qualified")
        if not base or base["exchange"] not in q["allowed_exchanges"]:
            reasons.append("exchange_execution_not_reviewed")
        rows = [
            r
            for r in self.records.get((day, key), [])
            if stamp(r["effective_from"]) <= time < stamp(r["effective_to"])
        ]
        if not rows:
            recorded = q.get("rule_rejections", {}).get(day + "/" + key, [])
            reasons.extend(recorded or ["exact_session_execution_rule_missing"])
        if reasons:
            return None, list(dict.fromkeys(reasons))
        row = rows[-1]
        if stamp(row["available_at"]) > time:
            return None, ["execution_parameter_not_yet_available"]
        meta = copy.copy(base)
        meta.update(
            {k: row[k] for k in ("tick_size", "value_per_price", "margin_rate")}
        )
        for field in ("min_open_lots", "daily_open_limit"):
            if field in row:
                meta[field] = row[field]
        meta["fees"] = [{"effective_from": day, "effective_to": day, **row["fees"]}]
        meta["execution_reference"] = {
            k: row[k]
            for k in (
                "source_date",
                "available_at",
                "margin_effective_at",
                "effective_from",
                "effective_to",
                "sources",
            )
        }
        meta["execution_reference"]["fee_model"] = q["fee_model"]
        for field in (
            "specification_available_at",
            "specification_effective_from",
            "specification_basis",
        ):
            if field in row:
                meta["execution_reference"][field] = row[field]
        for field in (
            "source_basis",
            "availability_basis",
            "min_open_lots",
            "daily_open_limit",
            "specification_source",
            "historical_specification_verified",
        ):
            if field in row:
                meta["execution_reference"][field] = row[field]
        return meta, []

    def candidate(self, row):
        meta, reasons = self.resolve(row["contract"], row["ranking_time"])
        return {
            **{
                k: row[k]
                for k in (
                    "date",
                    "group",
                    "product",
                    "contract",
                    "direction",
                    "rank",
                    "selected",
                    "ranking_time",
                )
            },
            "execution_pass": meta is not None and not reasons,
            "execution_rejections": reasons,
            "execution_reference": meta.get("execution_reference") if meta else None,
        }
