import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from quota_monitor.openai_client import OpenAIClientError
from quota_monitor.model_catalog import resource_path
from quota_monitor.server import create_server


ADMIN_KEY = "sk-admin-" + "z" * 12


class SuccessfulClient:
    def __init__(self, _key):
        pass

    def get(self, path, _params):
        if path.endswith("/completions"):
            return {
                "data": [
                    {
                        "results": [
                            {
                                "model": "gpt-5.4-mini-2026-03-17",
                                "service_tier": "incentivized-tier",
                                "input_tokens": 10,
                                "input_cached_tokens": 2,
                                "output_tokens": 1,
                                "project_id": "project-sensitive-value",
                                "organization_id": "organization-sensitive-value",
                            }
                        ]
                    }
                ],
                "next_page": None,
            }
        return {
            "data": [{"results": [{"amount": {"value": 0.25, "currency": "usd"}}]}],
            "next_page": None,
        }


class FailingClient:
    failure = None

    def __init__(self, _key):
        pass

    def get(self, *_args, **_kwargs):
        raise self.failure


@contextmanager
def running_server(client_factory=SuccessfulClient):
    server = create_server(client_factory=client_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, body


def test_server_binds_only_to_ipv4_loopback():
    with running_server() as server:
        assert server.server_address[0] == "127.0.0.1"


def test_catalog_has_version_no_store_and_browser_security_headers():
    with running_server() as server:
        status, headers, body = request(server, "/api/catalog")
    assert status == 200
    assert json.loads(body)["version"] == "0.1.0"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/../README.md", "/%2e%2e/README.md", "/..%5cREADME.md"])
def test_static_path_traversal_is_rejected_without_local_path_disclosure(path):
    with running_server() as server:
        status, _headers, body = request(server, path)
    assert status in {403, 404}
    assert b"Projects" not in body
    assert b"README" not in body


def test_untrusted_host_header_is_rejected():
    with running_server() as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
    assert response.status == 403
    assert body["error"]["code"] == "invalid_host"


@pytest.mark.parametrize(
    ("key", "expected_code"),
    [(None, "missing_admin_key"), ("sk-admin-short", "invalid_admin_key")],
)
def test_missing_and_malformed_keys_use_fixed_error_envelope(key, expected_code):
    headers = {} if key is None else {"X-Admin-Key": key}
    with running_server() as server:
        status, response_headers, body = request(server, "/api/data", headers)
    payload = json.loads(body)
    assert status == 400
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "request_id"}
    assert payload["error"]["code"] == expected_code
    assert payload["error"]["request_id"] == response_headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (OpenAIClientError("authentication_failed", "Check the Admin API Key.", 401), 401, "authentication_failed"),
        (OpenAIClientError("permission_denied", "Check organization permissions.", 403), 403, "permission_denied"),
        (OpenAIClientError("rate_limited", "Wait and retry.", 429), 429, "rate_limited"),
        (OpenAIClientError("upstream_unavailable", "Try again later.", 502), 502, "upstream_unavailable"),
        (OpenAIClientError("upstream_timeout", "Try again.", 504), 504, "upstream_timeout"),
        (OpenAIClientError("offline", "Check the network.", 503), 503, "offline"),
    ],
)
def test_upstream_failures_keep_status_and_safe_envelope(failure, status, code):
    FailingClient.failure = failure
    with running_server(FailingClient) as server:
        actual_status, _headers, body = request(server, "/api/data", {"X-Admin-Key": ADMIN_KEY})
    payload = json.loads(body)
    assert actual_status == status
    assert payload["error"]["code"] == code
    assert ADMIN_KEY not in body.decode()


def test_success_response_and_log_do_not_expose_secrets(capsys):
    with running_server() as server:
        status, _headers, body = request(server, "/api/data", {"X-Admin-Key": ADMIN_KEY})
    output = capsys.readouterr().out
    assert status == 200
    assert ADMIN_KEY not in body.decode()
    assert ADMIN_KEY not in output
    assert "project-sensitive-value" not in output
    assert "organization-sensitive-value" not in output
    assert "request_id=" in output


def test_page_source_does_not_use_browser_persistent_storage():
    source = resource_path("web", "js", "app.js").read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "indexedDB" not in source
