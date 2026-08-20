import pytest

from quota_monitor.upstream_adapter import (
    ProjectKeyDeriver,
    UNATTRIBUTED_PROJECT_KEY,
    UpstreamContractError,
    fetch_usage_slice,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        return self.responses.pop(0)


def bucket(**row_overrides):
    row = {
        "project_id": "project-private-a",
        "model": "gpt-5.6-terra",
        "service_tier": "incentivized-tier",
        "input_tokens": 100,
        "input_cached_tokens": 20,
        "output_tokens": 5,
        "num_model_requests": 2,
    }
    row.update(row_overrides)
    return {"start_time": 1000, "end_time": 2000, "results": [row]}


def test_project_keys_are_stable_private_pseudonyms_and_handle_unattributed():
    deriver = ProjectKeyDeriver(b"0123456789abcdef")

    first = deriver.derive("project-private-a")
    assert first == deriver.derive("project-private-a")
    assert first != ProjectKeyDeriver(b"different-secret!").derive("project-private-a")
    assert "private" not in first
    assert deriver.derive(None) == UNATTRIBUTED_PROJECT_KEY


def test_usage_adapter_paginates_and_preserves_project_and_bucket_dimensions():
    client = FakeClient(
        [
            {"data": [bucket()], "next_page": "page-2", "future": True},
            {"data": [bucket(project_id=None, model=None)], "next_page": None},
        ]
    )

    result = fetch_usage_slice(
        client,
        start_time=1000,
        end_time=2000,
        project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
    )

    assert result.pages_fetched == 2
    assert len(result.records) == 2
    assert result.records[0].project_key.startswith("project-")
    assert result.records[0].project_id_private == "project-private-a"
    assert "project-private-a" not in repr(result.records[0])
    assert result.records[1].project_key == "unattributed"
    assert result.records[1].model == "unknown"
    assert [params.get("page") for _, params in client.calls] == [None, "page-2"]


def test_usage_adapter_accepts_cache_write_breakdown_without_double_counting_total():
    client = FakeClient(
        [
            {
                "data": [bucket(input_tokens=100, input_cache_write_tokens=30)],
                "next_page": None,
            }
        ]
    )

    result = fetch_usage_slice(
        client,
        start_time=1000,
        end_time=2000,
        project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
    )

    assert result.records[0].input_tokens == 100


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"input_tokens": "100"}, "usage_field_invalid"),
        ({"input_tokens": 5, "input_cached_tokens": 6}, "usage_cached_tokens_invalid"),
        (
            {"input_tokens": 5, "input_cached_tokens": 0, "input_cache_write_tokens": 6},
            "usage_cache_write_tokens_invalid",
        ),
        ({"project_id": {"future": "shape"}}, "usage_project_invalid"),
        ({"service_tier": 123}, "usage_service_tier_invalid"),
    ],
)
def test_required_field_failures_have_stable_error_codes(override, code):
    client = FakeClient([{"data": [bucket(**override)], "next_page": None}])

    with pytest.raises(UpstreamContractError) as caught:
        fetch_usage_slice(
            client,
            start_time=1000,
            end_time=2000,
            project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
        )

    assert caught.value.code == code


def test_repeated_cursor_is_rejected_without_looping_forever():
    client = FakeClient(
        [
            {"data": [], "next_page": "same"},
            {"data": [], "next_page": "same"},
        ]
    )

    with pytest.raises(UpstreamContractError, match="repeated") as caught:
        fetch_usage_slice(
            client,
            start_time=1000,
            end_time=2000,
            project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
        )

    assert caught.value.code == "usage_cursor_repeated"
