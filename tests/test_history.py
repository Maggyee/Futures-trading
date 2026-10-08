import io
import json
from datetime import datetime

import pytest

from backend import history
from backend.market import TZ


def response(*rows):
    return "=(" + json.dumps(list(rows)) + ");"


def row(at="2026-09-29 09:01:00", **values):
    return {
        "d": at,
        "o": "3100",
        "h": "3102",
        "l": "3099",
        "c": "3101",
        "v": "100",
        "p": "1200",
        **values,
    }


def test_minute_end_is_converted_and_unfinished_bars_are_excluded():
    rows = history.normalize(
        response(row(), row("2026-09-29 09:02:00"), row("2026-09-29 09:03:00")),
        "rb2701.SHFE",
        now=datetime(2026, 9, 29, 9, 3, 30, tzinfo=TZ),
    )
    assert [r["datetime"] for r in rows] == [
        "2026-09-29T09:00:00+08:00",
        "2026-09-29T09:01:00+08:00",
    ]
    assert rows[0]["symbol"] == "rb2701"
    assert rows[0]["open_interest"] == "1200"


@pytest.mark.parametrize(
    "source",
    [
        response(row(h="3098")),
        response(row(v="-1")),
        "<html>failure</html>",
        "=(null);",
    ],
)
def test_invalid_source_data_is_rejected_before_import(source):
    with pytest.raises(ValueError):
        history.normalize(source, "rb2701.SHFE", now=datetime(2026, 9, 30, tzinfo=TZ))


def test_continuous_contract_alias_is_rejected():
    with pytest.raises(ValueError, match="具体交割合约"):
        history.normalize(response(row()), "RB0.SHFE")


def test_import_preserves_local_bars_and_saves_source_manifest(tmp_path, monkeypatch):
    raw = response(row(), row("2026-09-29 09:02:00"))
    monkeypatch.setattr(
        history.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(raw.encode()),
    )
    calls = []
    first_time = int(datetime(2026, 9, 29, 9, tzinfo=TZ).timestamp())

    def rpc(action, payload, key=None):
        calls.append((action, payload, key))
        if action == "bars":
            return {"bars": [{"time": first_time}]}
        assert action == "import"
        assert key
        bars = history.parse_csv(payload["csv"])
        assert len(bars) == 1
        assert bars[0].datetime.minute == 1
        return {"state": "completed", "result": {"count": 1}}

    monkeypatch.setattr(history, "rpc_call", rpc)
    result = history.import_symbol("rb2701.SHFE", tmp_path)
    assert result["existing_skipped"] == 1 and result["imported"] == 1
    assert result["state"] == "completed" and len(result["source_sha256"]) == 64
    manifest = json.loads((tmp_path / "rb2701.SHFE.manifest.json").read_text())
    assert manifest == result
    assert (tmp_path / "rb2701.SHFE.raw.txt").read_text() == raw


def test_second_import_with_all_timestamps_present_sends_no_write(
    tmp_path, monkeypatch
):
    raw = response(row())
    monkeypatch.setattr(
        history.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(raw.encode()),
    )

    def rpc(action, payload, key=None):
        assert action == "bars"
        return {
            "bars": [{"time": int(datetime(2026, 9, 29, 9, tzinfo=TZ).timestamp())}]
        }

    monkeypatch.setattr(history, "rpc_call", rpc)
    result = history.import_symbol("rb2701.SHFE", tmp_path)
    assert result["imported"] == 0 and result["existing_skipped"] == 1
