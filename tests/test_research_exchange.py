"""Actual official fee examples, kept independent of synthetic price paths."""

import copy
import json
from pathlib import Path

import pytest

from research.config import ResearchError
from research.data import file_sha256
from research.exchange_diagnostic import (
    PublicArchive,
    cffex_parameters,
    czce_parameters,
    gfex_parameters,
    number,
)
from research.execution import fee


@pytest.fixture(scope="module")
def reference():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/exchange_execution_reference.json"
        ).read_text()
    )


def costing(rule, value_per_price, day, side, price, quantity=1):
    meta = {
        "symbol": "ARITHMETIC_ONLY",
        "value_per_price": value_per_price,
        "fees": [{"effective_from": day, "effective_to": day, **rule["fees"]}],
    }
    return fee(meta, day, side, price, quantity)


def test_czce_actual_fixed_and_turnover_fees(reference):
    raw = reference["czce"]
    rules = czce_parameters(raw["text"], raw["day"])
    assert costing(rules["FG701"], 20, raw["day"], "open", 984, 3) == 6
    assert costing(rules["FG701"], 20, raw["day"], "close_today", 985, 3) == 6
    assert costing(rules["AP701"], 10, raw["day"], "open", 7494) == 5
    assert costing(rules["AP701"], 10, raw["day"], "close_today", 7495) == 10
    # Raw "1.00 比例值" = 1 / 10000 of 9362 * 5 * 2; same-day closing is free.
    assert costing(rules["PX611"], 5, raw["day"], "open", 9362, 2) == pytest.approx(
        9.362
    )
    assert costing(rules["PX611"], 5, raw["day"], "close_today", 9364, 2) == 0
    assert rules["FG701"]["daily_open_limit"] == 25000
    assert rules["AP701"]["margin_rate"] == 0.09


def test_cffex_actual_close_today_multiplier_is_ten(reference):
    raw = reference["cffex"]
    # Footer notes exist in the real CSV and must not be parsed as a contract.
    rules = cffex_parameters(raw["text"] + "说明：手续费标准见通知\n", raw["day"])
    for symbol in ("IC2612", "IM2612"):
        assert costing(rules[symbol], 200, raw["day"], "open", 6000) == pytest.approx(
            27.6
        )
        assert costing(
            rules[symbol], 200, raw["day"], "close_today", 6000
        ) == pytest.approx(276)
    for symbol in ("TF2612", "TL2612"):
        assert costing(rules[symbol], 10000, raw["day"], "open", 100, 2) == 6
        assert costing(rules[symbol], 10000, raw["day"], "close_today", 100.01, 2) == 0
    assert rules["TF2612"]["margin_rate"] == 0.012
    assert rules["TL2612"]["margin_rate"] == 0.035


@pytest.mark.parametrize(
    "date,price,opening,today",
    [("20260911", 133960, 21.4336, 42.8672), ("20260922", 133160, 10.6528, 10.6528)],
)
def test_gfex_actual_before_after_fee_change(reference, date, price, opening, today):
    day = date[:4] + "-" + date[4:6] + "-" + date[6:]
    rule = gfex_parameters(reference["gfex"][date]["document"], day)["lc2701"]
    assert costing(rule, 1, day, "open", price) == pytest.approx(opening)
    assert costing(rule, 1, day, "close_today", price) == pytest.approx(today)
    assert rule["margin_rate"] == 0.15


@pytest.mark.parametrize("exchange", ["czce", "cffex", "gfex"])
def test_wrong_date_or_error_body_is_rejected(reference, exchange):
    if exchange == "gfex":
        with pytest.raises(ResearchError, match="日期错配"):
            gfex_parameters(reference["gfex"]["20260911"]["document"], "2026-09-14")
    else:
        parser = czce_parameters if exchange == "czce" else cffex_parameters
        with pytest.raises(ResearchError, match="日期或表名错配"):
            parser(reference[exchange]["text"], "2026-09-30")
        with pytest.raises(ResearchError):
            parser("<html>Access denied</html>", reference[exchange]["day"])


@pytest.mark.parametrize(
    "mutation", ["unknown_units", "short_open_fee", "server_error"]
)
def test_gfex_does_not_guess_units_or_different_intraday_open_fee(reference, mutation):
    doc = copy.deepcopy(reference["gfex"]["20260911"]["document"])
    if mutation == "unknown_units":
        doc["data"][0]["style"] = "人民币"
    elif mutation == "short_open_fee":
        doc["data"][0]["shortOpenFee"] = 3.2
    else:
        doc["code"] = "1"
    with pytest.raises(ResearchError):
        gfex_parameters(doc, "2026-09-11")


@pytest.mark.parametrize("value", ["", "not-a-number", "NaN", "Infinity", "-1"])
def test_invalid_official_numbers_cannot_become_fees(value):
    with pytest.raises(ResearchError):
        number(value)


@pytest.mark.parametrize("mutation", ["hash", "redirect", "credential", "incomplete"])
def test_archive_requires_complete_original_official_response(tmp_path, mutation):
    doc = tmp_path / "parameter.txt"
    doc.write_text("official body")
    row = {
        "path": str(doc),
        "status": 200,
        "sha256": file_sha256(doc),
        "url": "https://www.czce.com.cn/parameter.txt",
        "credential_used": False,
        "checked_utc": "2026-10-03T00:00:00+00:00",
        "bytes": doc.stat().st_size,
    }
    if mutation == "hash":
        doc.write_text("changed body")
    elif mutation == "redirect":
        row["final_url"] = "https://unofficial.example/parameter.txt"
    elif mutation == "credential":
        row["credential_used"] = True
    else:
        row["error"] = "incomplete chunked read"
    (tmp_path / "probe_receipts.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ResearchError):
        PublicArchive(tmp_path).read(doc.name, "www.czce.com.cn")
