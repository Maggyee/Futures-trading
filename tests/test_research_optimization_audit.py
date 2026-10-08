import csv
import gzip
import hashlib
import io
import json
import tarfile

import pytest

from research.calendar import MINUTE, at
from research.config import ResearchError, digest
from research.optimization_audit import afternoon_requirements, audit_afternoon, audit_pullbacks, audit_source_archive


DAY = "2026-01-08"


def test_source_archive_audit_rejects_a_changed_member_even_with_a_valid_manifest(tmp_path):
    name, declared = "research/example.py", b"value = 1\n"
    hashes = {name: hashlib.sha256(declared).hexdigest()}
    (tmp_path/"manifest.json").write_text(json.dumps({"source_hashes": hashes, "code_hash": digest(hashes)}))
    def write(body):
        with tarfile.open(tmp_path/"source_snapshot.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo(name); info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    write(declared)
    assert audit_source_archive(tmp_path)["files_checked"] == 1
    write(b"value = 2\n")
    with pytest.raises(ResearchError, match="来源指纹不符"):
        audit_source_archive(tmp_path)


def archive(path, records):
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def test_independent_afternoon_audit_rejects_future_return_and_missing_observation(tmp_path):
    keys = ["aa2603.SHFE", "bb2603.SHFE", "cc2603.SHFE"]
    cfg = {"strategy": {"k": 2, "afternoon_rerank": {"opening_minutes": 8}},
           "metadata": {"contracts": [{"symbol": key.split('.')[0], "exchange": "SHFE", "session_profile": "day"} for key in keys]},
           "calendar": {"trading_days": [DAY], "profiles": {"day": [["09:00", "11:30"], ["13:30", "15:00"]]}}}
    pool = [{"date": DAY, "contract": key, "group": "commodity", "previous_volume": 100,
             "previous_oi": 1000, "selection_day": "2026-01-07"} for key in keys]
    archive(tmp_path/"daily_pool.csv.gz", pool)
    opening = at(DAY, "13:30")
    raw = {(key, (opening+i*MINUTE).isoformat()): {"open": 100, "close": 101, "volume": 1, "open_interest": 1000, "tradable": True}
           for key in keys for i in range(8) if not (key == keys[2] and i == 7)}
    raw[(keys[0], (opening+8*MINUTE).isoformat())] = {"close": 50000}
    candidates = [pool[i] | {"direction": "LONG", "r8": 0.01, "rank": i+1, "selected": True, "k": 2,
                            "opening": opening.isoformat(), "ranking_time": (opening+8*MINUTE).isoformat(),
                            "ranking_phase": "afternoon", "opening_price_definition": "afternoon_first_minute_open"} for i in range(2)]
    archive(tmp_path/"daily_candidates.csv.gz", candidates)
    cohorts, needed = afternoon_requirements(tmp_path, cfg)
    assert len(needed) == 24
    result = audit_afternoon(tmp_path, cfg, cohorts, raw, [])
    assert result["candidates_checked"] == 2 and result["unavailable"][0]["contract"] == keys[2]
    archive(tmp_path/"daily_candidates.csv.gz", [candidates[0] | {"r8": 499}, candidates[1]])
    with pytest.raises(ResearchError, match="r8"):
        audit_afternoon(tmp_path, cfg, cohorts, raw, [])
    archive(tmp_path/"daily_candidates.csv.gz", candidates + [candidates[0] | {"contract": keys[2]}])
    with pytest.raises(ResearchError, match="不可用"):
        audit_afternoon(tmp_path, cfg, cohorts, raw, [])


def test_independent_pullback_audit_checks_confirmed_event_and_single_consumption():
    key = "aa2603.SHFE"
    data = [{"end": (at(DAY, "09:00")+i*MINUTE).isoformat(), "close": 80+i, "high": 80+i+0.5,
             "low": 80+i-0.5, "previous_atr": 4} for i in range(30)]
    for i, row in enumerate(data):
        for period in (10, 20):
            row["ma"+str(period)] = sum(r["close"] for r in data[max(0,i-period+1):i+1])/min(period,i+1)
    data[28]["low"] = data[28]["ma10"]
    end = data[29]["end"]
    event = {"event": data[28]["end"], "references": [10], "dual_touch": False, "epsilon": 0.5}
    cfg = {"strategy": {"entry_mode": "pullback_ma10", "pullback_epsilon_ticks": 0.5, "pullback_epsilon_atr": 0.1},
           "metadata": {"contracts": [{"symbol": "aa2603", "exchange": "SHFE", "tick_size": 1}]}}
    trade = {"contract": key, "entry_signal_time": end, "direction": "LONG", "pullback": json.dumps(event)}
    signals = {(key,end): {"pullback": json.dumps(event)}}
    args = (signals, {key: data}, {key: {r["end"]: i for i,r in enumerate(data)}})
    assert audit_pullbacks(cfg, [trade], *args)["entries_checked"] == 1
    with pytest.raises(ResearchError, match="重复消费"):
        audit_pullbacks(cfg, [trade, trade], *args)
    with pytest.raises(ResearchError, match="事件不符"):
        audit_pullbacks(cfg, [trade | {"pullback": json.dumps(event | {"event": end})}], *args)
    data[29]["close"] = data[28]["high"]
    with pytest.raises(ResearchError, match="突破"):
        audit_pullbacks(cfg, [trade], *args)
