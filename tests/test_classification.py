import pytest

from quota_monitor.classification import cost_level, estimate_cost, is_incentivized


@pytest.mark.parametrize(
    ("service_tier", "expected"),
    [
        (None, False),
        ("", False),
        ("default", False),
        ("incentivized-tier", True),
        ("INCENTIVIZED-TIER", True),
        ("data-sharing", True),
        ("data_sharing", True),
        ("not-incentivized", False),
        ("future-tier", False),
    ],
)
def test_incentivized_tier_is_an_explicit_allowlist(service_tier, expected):
    assert is_incentivized(service_tier) is expected


def test_cost_calculation_does_not_double_count_cached_tokens():
    pricing = {"input": 2.5, "cached_input": 0.25, "output": 15}
    value = estimate_cost(1000, 400, 100, pricing)
    expected = (600 * 2.5 + 400 * 0.25 + 100 * 15) / 1_000_000
    assert value == expected


def test_cost_calculation_clamps_invalid_token_counts():
    pricing = {"input": 2.5, "cached_input": 0.25, "output": 15}
    assert estimate_cost(100, 500, -20, pricing) == 100 * 0.25 / 1_000_000


def test_cost_level():
    assert cost_level({"input": 0.2, "output": 1.25}) == "low"
    assert cost_level({"input": 0.75, "output": 4.5}) == "medium"
    assert cost_level({"input": 5, "output": 30}) == "high"
