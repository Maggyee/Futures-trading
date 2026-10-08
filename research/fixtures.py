"""Deterministic engineering fixtures, never evidence of strategy profitability."""

import copy
import csv
import json
from datetime import date, timedelta
from pathlib import Path

from .calendar import Calendar
from .config import read_config


def create_fixture(target):
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).parent / "examples"
    cfg = json.loads((example / "config.json").read_text())
    cal = json.loads((example / "calendar.json").read_text())
    days = [(date(2026, 1, 5) + timedelta(days=i)).isoformat() for i in range(5)]
    cal.update(trading_days=days, verified=True, source="SYNTHETIC_TEST_ONLY")
    cfg.update(synthetic=True, metadata="metadata.json", calendar="calendar.json")
    cfg["data"]["sources"] = [
        {"format": "csv", "path": "bars.csv", "provenance": "SYNTHETIC_TEST_ONLY"}
    ]
    cfg["strategy"]["approximate_vwap"] = False
    contracts = []
    for product, group, direction in [
        ("aa", "commodity", 1),
        ("bb", "commodity", -1),
        ("cc", "financial", 1),
        ("dd", "financial", -1),
    ]:
        contracts.append(
            {
                "symbol": product + "2603",
                "exchange": "SHFE" if group == "commodity" else "CFFEX",
                "product": product,
                "group": group,
                "session_profile": "commodity" if group == "commodity" else "index",
                "time_profile": "commodity" if group == "commodity" else "index",
                "listed": "2025-01-01",
                "expiry": "2026-03-31",
                "effective_from": "2025-01-01",
                "tick_size": 0.01,
                "value_per_price": 10,
                "turnover_factor": 10,
                "margin_rate": 0.1,
                "verified": True,
                "fees": [
                    {
                        "effective_from": "2025-01-01",
                        "open": {"mode": "fixed", "value": 1},
                        "close_today": {"mode": "fixed", "value": 2},
                        "close_yesterday": {"mode": "rate", "value": 0.0001},
                    }
                ],
                "fixture_direction": direction,
            }
        )
    cfg["strategy"]["fixed_ticks"] = {
        r["product"]: {"stop_loss_ticks": 200, "take_profit_ticks": 400}
        for r in contracts
    }
    cfg["risk"]["max_lots_per_contract"] = 3
    cfg["splits"] = {
        "train": {"start": days[0], "end": days[2]},
        "validation": {"start": days[3], "end": days[3]},
        "test": {"start": days[4], "end": days[4]},
    }
    cfg["experiments"]["windows"] = [
        {
            "train": copy.deepcopy(cfg["splits"]["train"]),
            "validation": copy.deepcopy(cfg["splits"]["validation"]),
        }
    ]
    (target / "calendar.json").write_text(json.dumps(cal, indent=2))
    (target / "metadata.json").write_text(
        json.dumps({"source": "SYNTHETIC_TEST_ONLY", "contracts": contracts}, indent=2)
    )
    (target / "config.json").write_text(json.dumps(cfg, indent=2))
    fields = [
        "datetime",
        "trading_day",
        "exchange",
        "symbol",
        "product",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "open_interest",
        "provenance",
    ]
    with (target / "bars.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for contract in contracts:
            price = 100
            for day in days:
                for n, dt in enumerate(Calendar(cal).minutes(day, contract)):
                    op = round(price, 2)
                    price = round(price + contract["fixture_direction"] * 0.03, 2)
                    high, low = (
                        round(max(op, price) + 0.3, 2),
                        round(min(op, price) - 0.3, 2),
                    )
                    volume = 400 if dt.strftime("%H:%M") == "13:40" else 100
                    writer.writerow(
                        {
                            "datetime": dt.isoformat(),
                            "trading_day": day,
                            "exchange": contract["exchange"],
                            "symbol": contract["symbol"],
                            "product": contract["product"],
                            "open": op,
                            "high": high,
                            "low": low,
                            "close": price,
                            "volume": volume,
                            "turnover": (high + low + price) / 3 * volume * 10,
                            "open_interest": 10000 + n * 2,
                            "provenance": "SYNTHETIC_TEST_ONLY",
                        }
                    )
    return read_config(target / "config.json")
