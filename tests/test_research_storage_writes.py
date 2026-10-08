"""Larger JSON write batches retain exact content and pre-write disk limits."""

import json

import pytest

from research.config import ResearchError
from research.storage import SpaceBudget, write_bounded_json


def test_batched_json_matches_unicode_bytes_and_charges_before_writing(tmp_path):
    value = {"观察": [{"合约": "测试合约", "数值": 123.456}] * 22000}
    expected = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    target = tmp_path / "observations.json"
    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": len(expected), "min_free_bytes": 0}
    )
    original = budget.check
    sizes = []

    def check(path=None, reserve=0):
        if reserve:
            sizes.append(reserve)
        return original(path, reserve)

    budget.check = check
    write_bounded_json(target, value, budget)
    assert target.read_bytes() == expected
    assert sum(sizes) == len(expected)


def test_batched_json_never_writes_bytes_past_shared_budget(tmp_path):
    (tmp_path / "other.bin").write_bytes(b"x" * 100)
    target = tmp_path / "partial.json"
    budget = SpaceBudget(
        {"roots": [str(tmp_path)], "max_bytes": 1100100, "min_free_bytes": 0}
    )
    with pytest.raises(ResearchError, match="空间预算"):
        write_bounded_json(target, ["中文" * 20] * 30000, budget)
    assert target.stat().st_size + 100 <= 1100100
