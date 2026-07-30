import io
import json
import socket
from urllib.error import HTTPError, URLError

import pytest

from quota_monitor.openai_client import OpenAIAdminClient, OpenAIClientError


ADMIN_KEY = "sk-admin-" + "x" * 12


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


@pytest.mark.parametrize("key", ["", "sk-project-short", "sk-admin-short"])
def test_admin_key_format_is_validated_before_network(key):
    with pytest.raises(OpenAIClientError) as caught:
        OpenAIAdminClient(key)
    assert caught.value.code == "invalid_admin_key"
    assert caught.value.http_status == 400


@pytest.mark.parametrize(
    ("status", "code", "local_status"),
    [
        (401, "authentication_failed", 401),
        (403, "permission_denied", 403),
        (429, "rate_limited", 429),
        (500, "upstream_unavailable", 502),
        (503, "upstream_unavailable", 502),
    ],
)
def test_http_errors_are_mapped_without_leaking_key(monkeypatch, status, code, local_status):
    def fail(*_args, **_kwargs):
        raise HTTPError(
            "https://api.openai.com/v1/organization/costs",
            status,
            "upstream detail",
            {},
            io.BytesIO(json.dumps({"error": {"message": ADMIN_KEY}}).encode()),
        )

    monkeypatch.setattr("quota_monitor.openai_client.urlopen", fail)
    with pytest.raises(OpenAIClientError) as caught:
        OpenAIAdminClient(ADMIN_KEY).get("/organization/costs", {"start_time": 1})
    assert caught.value.code == code
    assert caught.value.http_status == local_status
    assert ADMIN_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (socket.timeout("slow"), "upstream_timeout", 504),
        (TimeoutError("slow"), "upstream_timeout", 504),
        (URLError("offline secret"), "offline", 503),
    ],
)
def test_network_errors_are_actionable_and_sanitized(monkeypatch, failure, code, status):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("quota_monitor.openai_client.urlopen", fail)
    with pytest.raises(OpenAIClientError) as caught:
        OpenAIAdminClient(ADMIN_KEY).get("/organization/costs", {"start_time": 1})
    assert caught.value.code == code
    assert caught.value.http_status == status
    assert "secret" not in str(caught.value)


def test_invalid_json_is_mapped_to_safe_response_error(monkeypatch):
    monkeypatch.setattr(
        "quota_monitor.openai_client.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"not-json"),
    )
    with pytest.raises(OpenAIClientError) as caught:
        OpenAIAdminClient(ADMIN_KEY).get("/organization/costs", {"start_time": 1})
    assert caught.value.code == "upstream_response_invalid"
    assert caught.value.http_status == 502
