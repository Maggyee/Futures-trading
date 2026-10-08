import io
import json

from research.signal_journal import SignalJournal
from research.storage import SpaceBudget, write_gzip_json, read_result
from research.reporting import funnel, rank_contribution


def test_journal_retains_later_fill_updates_and_preserves_full_row_order(tmp_path):
    budget = SpaceBudget({"roots": [str(tmp_path)], "max_bytes": 1000000, "min_free_bytes": 0})
    rows = SignalJournal(tmp_path / "journal.partial", budget)
    rejected = {"trigger": False, "rank": 1, "filled": False}
    pending = {"trigger": True, "rank": 2, "filled": False}
    later = {"trigger": False, "rank": 3, "filled": False}
    rows.append(rejected); rows.append(pending); rows.append(later)
    pending.update(filled=True, fill_time="next tradable minute")
    expected = [rejected, pending, later]
    assert len(rows) == 3 and list(rows) == expected and list(rows) == expected
    out = io.StringIO(); json.dump({"signals": rows}, out)
    assert json.loads(out.getvalue())["signals"] == expected
    write_gzip_json(tmp_path / "result.json.gz", {"signals": rows}, budget)
    assert read_result(tmp_path)["signals"] == expected
    rows.discard()
    assert not (tmp_path / "journal.partial").exists()


def test_monthly_funnel_and_rank_statistics_accept_one_pass_observations():
    class OnePass:
        def __init__(self, rows):
            self.rows, self.used = rows, False

        def __iter__(self):
            assert not self.used, "Monthly observations must not be scanned repeatedly"
            self.used = True
            yield from self.rows

        def __len__(self):
            raise AssertionError("Do not materialize the monthly observation journal")

    filters = {key: True for key in ["candidate", "warmup_1m", "warmup_higher",
               "current_session_15m", "trend_15m", "trend_5m", "trend_1m", "vwap",
               "oi", "efficiency", "shock", "extension", "state"]}
    base = {"rank": 1, "trigger": True, "risk_pass": False, "filled": False,
            "filters": filters, "rejections": [], "risk_rejections": []}
    observations = [
        {**base, "risk_pass": True, "filled": True},
        {**base, "rank": 2, "execution_pass": False, "execution_rejections": ["missing_rule"]},
        {**base, "risk_rejections": ["group_margin"]},
        {**base, "rank": 2, "trigger": False, "filters": {**filters, "oi": False}, "rejections": ["oi"]},
        {**base, "rank": 2, "trigger": False, "filters": {**filters, "candidate": False, "state": False}},
    ]
    review = funnel(OnePass(observations))
    assert review["sequential"] == {"candidate": 4, "trend": 4, "vwap": 4,
                                    "oi": 3, "smooth": 3, "entry_trigger": 3,
                                    "execution_pass": 2, "risk_pass": 1, "actual_fill": 1}
    assert review["independent_rejections"] == {"missing_rule": 1, "group_margin": 1, "oi": 1}
    ranked = rank_contribution({"signals": OnePass(observations),
                               "daily_candidates": [{"rank": 1}, {"rank": 2}, {"rank": 3}],
                               "trades": [{"rank": 1, "net_pnl": 125.5}]})
    assert ranked == [
        {"rank": 1, "qualified_minutes_without_candidate_or_state_gate": 2,
         "trigger_count": 2, "execution_qualified_triggers": 2, "execution_rejections": 0,
         "risk_rejections": 1, "trade_count": 1, "net_pnl": 125.5},
        {"rank": 2, "qualified_minutes_without_candidate_or_state_gate": 2,
         "trigger_count": 1, "execution_qualified_triggers": 0, "execution_rejections": 1,
         "risk_rejections": 0, "trade_count": 0, "net_pnl": 0},
        {"rank": 3, "qualified_minutes_without_candidate_or_state_gate": 0,
         "trigger_count": 0, "execution_qualified_triggers": 0, "execution_rejections": 0,
         "risk_rejections": 0, "trade_count": 0, "net_pnl": 0},
    ]
