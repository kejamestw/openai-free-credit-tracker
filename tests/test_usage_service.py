import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quota_monitor.model_catalog import load_catalog
from quota_monitor.usage_service import UsageDataError, fetch_usage, summarize_usage, utc_day_range


CATALOG = load_catalog()


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params.copy()))
        return self.responses.pop(0)


def usage_row(**overrides):
    row = {
        "model": "gpt-5.4-mini-2026-03-17",
        "service_tier": "incentivized-tier",
        "input_tokens": 1000,
        "input_cached_tokens": 200,
        "output_tokens": 100,
    }
    row.update(overrides)
    return row


def test_utc_day_range_at_midnight():
    midnight = datetime(2026, 7, 31, tzinfo=timezone.utc)
    start, end = utc_day_range(midnight)
    assert start == end == int(midnight.timestamp())


def test_fetch_usage_at_midnight_returns_empty_without_zero_length_api_request():
    client = FakeClient([])
    midnight = datetime(2026, 7, 31, tzinfo=timezone.utc)
    result = fetch_usage(client, CATALOG, midnight)
    assert result["groups"]["mini"]["total"] == 0
    assert result["start"] == result["end"]
    assert client.calls == []


def test_utc_day_range_converts_non_utc_input_across_dates():
    taipei = timezone(timedelta(hours=8))
    local_time = datetime(2026, 7, 31, 0, 30, tzinfo=taipei)
    start, end = utc_day_range(local_time)
    assert start == int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp())
    assert end == int(datetime(2026, 7, 30, 16, 30, tzinfo=timezone.utc).timestamp())


def test_utc_day_range_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_day_range(datetime(2026, 7, 31, 12, 0))


def test_usage_pagination_counts_each_page_once():
    client = FakeClient(
        [
            {
                "data": [{"results": [usage_row(input_tokens=100, input_cached_tokens=0)]}],
                "next_page": "page-2",
            },
            {
                "data": [{"results": [usage_row(input_tokens=200, input_cached_tokens=0)]}],
                "next_page": None,
            },
        ]
    )
    result = fetch_usage(client, CATALOG, datetime(2026, 7, 31, 12, tzinfo=timezone.utc))
    assert result["groups"]["mini"]["total"] == 500
    assert [call[1].get("page") for call in client.calls] == [None, "page-2"]


def test_usage_pagination_rejects_repeated_cursor():
    client = FakeClient(
        [
            {"data": [], "next_page": "same-page"},
            {"data": [], "next_page": "same-page"},
        ]
    )
    with pytest.raises(UsageDataError, match="repeated pagination cursor"):
        fetch_usage(client, CATALOG)


def test_service_tiers_cached_output_unknown_models_and_empty_bucket():
    buckets = [
        {"results": []},
        {
            "results": [
                usage_row(input_tokens=100, input_cached_tokens=40, output_tokens=20),
                usage_row(service_tier="default", input_tokens=30, input_cached_tokens=0, output_tokens=5),
                usage_row(service_tier=None, input_tokens=10, input_cached_tokens=0, output_tokens=2),
                usage_row(service_tier="future-tier", input_tokens=7, input_cached_tokens=0, output_tokens=3),
                usage_row(model="future-model", input_tokens=50, input_cached_tokens=10, output_tokens=5),
            ]
        },
    ]
    result = summarize_usage(buckets, CATALOG)
    mini = result["groups"]["mini"]
    assert mini == {
        "input": 100,
        "cached_input": 40,
        "output": 20,
        "total": 120,
        "models": {
            "gpt-5.4-mini": {"input": 100, "cached_input": 40, "output": 20, "total": 120}
        },
    }
    assert result["other_usage"] == {
        "input": 97,
        "cached_input": 10,
        "output": 15,
        "total": 112,
    }
    assert result["unpriced_tokens"] == 55
    assert result["list_price_estimate_usd"] > 0


def test_only_incentivized_usage_enters_main_cards():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "usage_incentivized.json").read_text(encoding="utf-8")
    )
    result = summarize_usage(fixture, CATALOG)
    assert result["groups"]["mini"]["total"] == 1100
    assert result["other_usage"]["total"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [{}]},
        {"data": [{"results": None}]},
    ],
)
def test_usage_rejects_missing_required_response_fields(payload):
    with pytest.raises(UsageDataError, match="Usage API response"):
        fetch_usage(FakeClient([payload]), CATALOG)


def test_usage_ignores_unknown_fields_but_rejects_invalid_token_fields():
    valid = usage_row(future_field={"safe": True})
    assert summarize_usage([{"results": [valid], "future_bucket_field": 1}], CATALOG)["groups"]["mini"][
        "total"
    ] == 1100

    invalid = usage_row(input_tokens="1000")
    with pytest.raises(UsageDataError, match="input_tokens"):
        summarize_usage([{"results": [invalid]}], CATALOG)
