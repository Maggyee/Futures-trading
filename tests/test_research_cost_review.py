"""SYNTHETIC_TEST_ONLY: official-field units, cache integrity, no backtest claims."""

import copy
import json

import pytest

from research.config import ResearchError
from research.cost_review import (
    PARAMETERS,
    fee_reference,
    fetch_parameter,
    parameter_rows,
    review_costs,
)
from research.data import file_sha256
from research.fixtures import create_fixture
from research.storage import SpaceBudget


def documents(day="20260914"):
    base = {
        "report_date": day,
        "ContractBaseInfo": [
            {
                "INSTRUMENTID": "rb2701",
                "EXCHANGEID": "SHFE",
                "COMMODITYID": "rb",
                "TRADINGDAY": day,
                "OPENDATE": "20260116",
                "EXPIREDATE": "20270115",
            }
        ],
    }
    settlement = {
        "report_date": day,
        "Settlement": [
            {
                "INSTRUMENTID": "rb2701",
                "TRADINGDAY": day,
                "TRADEFEERATION": "0.00005",
                "TRADEFEEUNIT": "0",
                "DISCOUNTRATE": "2",
                "SPEC_LONGMARGINRATIO": "0.07",
                "SPEC_SHORTMARGINRATIO": "0.07",
            }
        ],
    }
    return base, settlement


@pytest.mark.parametrize("discount,expected", [("0", 0), ("1", 0.00005), ("2", 0.0001)])
def test_reference_fraction_and_close_today_are_not_converted_twice(discount, expected):
    _, doc = documents()
    doc["Settlement"][0]["DISCOUNTRATE"] = discount
    fees = fee_reference(doc["Settlement"][0])
    assert fees["open"] == {"mode": "rate", "value": 0.00005}
    assert fees["close_today"] == {"mode": "rate", "value": expected}
    assert fees["close_yesterday"] == fees["open"]


def test_fixed_reference_and_historical_dates():
    base, settlement = documents()
    settlement["Settlement"][0].update(TRADEFEERATION="0", TRADEFEEUNIT="3")
    row = parameter_rows(base, settlement, {"rb": "SHFE"})[0]
    assert row["exchange_fee_reference"]["close_today"] == {"mode": "fixed", "value": 6}
    assert row["listed"] == "2026-01-16" and row["last_trade_date"] == "2027-01-15"
    assert row["coverage_start"] == row["coverage_end"] == "2026-09-14"
    assert row["exchange_speculation_margin"] == {"long": 0.07, "short": 0.07}
    assert row["reference_only"] and row["broker_markup"] is None


def test_reference_rejects_date_and_contract_mismatches():
    base, settlement = documents()
    with pytest.raises(ResearchError, match="同一日期"):
        parameter_rows(base, {**settlement, "report_date": "20260915"}, {"rb": "SHFE"})
    other = copy.deepcopy(settlement)
    other["Settlement"][0]["TRADINGDAY"] = "20260915"
    with pytest.raises(ResearchError, match="日期"):
        parameter_rows(base, other, {"rb": "SHFE"})
    other = copy.deepcopy(settlement)
    other["Settlement"][0]["INSTRUMENTID"] = "rb2702"
    with pytest.raises(ResearchError, match="同日官方合约目录"):
        parameter_rows(base, other, {"rb": "SHFE"})


def test_unsupported_mixed_fees_and_asymmetric_margin_do_not_silently_simplify():
    base, settlement = documents()
    settlement["Settlement"][0].update(TRADEFEEUNIT="1", SPEC_SHORTMARGINRATIO="0.09")
    row = parameter_rows(base, settlement, {"rb": "SHFE"})[0]
    assert row["exchange_fee_reference"] is None
    assert len(row["issues"]) == 2
    assert row["exchange_speculation_margin"] == {"long": 0.07, "short": 0.09}
    with pytest.raises(ResearchError, match="非有限"):
        fee_reference({**settlement["Settlement"][0], "TRADEFEEUNIT": "NaN"})


def test_cached_official_parameters_require_hash_url_and_exact_date(tmp_path):
    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": 100000, "min_free_bytes": 0}
    )
    path = tmp_path / "ContractBaseInfo20260914.json"
    path.write_text(json.dumps(documents()[0]))
    receipt = path.with_suffix(".json.receipt.json")
    receipt.write_text(
        json.dumps(
            {
                "url": PARAMETERS["ContractBaseInfo"].format(day="20260914"),
                "sha256": file_sha256(path),
            }
        )
    )
    assert (
        fetch_parameter("2026-09-14", "ContractBaseInfo", tmp_path, budget)[0][
            "report_date"
        ]
        == "20260914"
    )
    path.write_text(json.dumps(documents("20260915")[0]))
    with pytest.raises(ResearchError, match="SHA256"):
        fetch_parameter("2026-09-14", "ContractBaseInfo", tmp_path, budget)
    saved = json.loads(receipt.read_text())
    saved["sha256"] = file_sha256(path)
    receipt.write_text(json.dumps(saved))
    with pytest.raises(ResearchError, match="缓存.*日期"):
        fetch_parameter("2026-09-14", "ContractBaseInfo", tmp_path, budget)


def test_ine_products_keep_true_exchange_even_when_publisher_id_is_shfe():
    base, settlement = documents()
    base["ContractBaseInfo"][0].update(INSTRUMENTID="sc2610", COMMODITYID="sc")
    settlement["Settlement"][0]["INSTRUMENTID"] = "sc2610"
    row = parameter_rows(base, settlement, {"sc": "INE"})[0]
    assert row["contract"] == "sc2610.INE" and row["product_id"] == "INE.sc"
    assert row["reported_exchange_id"] == "SHFE"
    unknown = parameter_rows(base, settlement, {})[0]
    assert unknown["contract"] is None and unknown["issues"]


def test_offline_cost_review_preserves_configuration_and_fee_activation(tmp_path):
    cfg = create_fixture(tmp_path / "SYNTHETIC_TEST_ONLY_inputs")
    cfg["storage"] = {
        "budget": {"roots": [str(tmp_path)], "max_bytes": 10000000, "min_free_bytes": 0}
    }
    path = tmp_path / "SYNTHETIC_TEST_ONLY_config.json"
    path.write_text(json.dumps(cfg))
    original = file_sha256(path)
    sources = tmp_path / "sources"
    sources.mkdir()
    for day in cfg["calendar"]["trading_days"]:
        for table, doc in zip(PARAMETERS, documents(day.replace("-", "")), strict=True):
            target = sources / (table + day.replace("-", "") + ".json")
            target.write_text(json.dumps(doc))
            target.with_suffix(".json.receipt.json").write_text(
                json.dumps(
                    {
                        "url": PARAMETERS[table].format(day=day.replace("-", "")),
                        "sha256": file_sha256(target),
                    }
                )
            )
    result = review_costs(path, sources, tmp_path / "out", fetch=False)
    assert result["unavailable_days"] == 0 and result["contract_days"] == 5
    assert file_sha256(path) == original
    saved = json.loads((tmp_path / "out" / "exchange_reference.json").read_text())
    assert saved["configuration_modified"] is False
    assert saved["ready_for_formal_backtest"] is False
