import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from quota_monitor import __version__
from quota_monitor.config_service import (
    AppConfig,
    ConfigService,
    ConfigWriteError,
    NetworkSettings,
    StartupSettings,
    UpdateSettings,
    config_to_dict,
)
from quota_monitor.openai_client import OpenAIClientError
from quota_monitor.model_catalog import resource_path
from quota_monitor.platform_paths import AppPaths
from quota_monitor.platform_adapters import InMemoryStartupAdapter
from quota_monitor.runtime import RuntimeServices
from quota_monitor.server import create_server
from quota_monitor.semver import SemVer
from quota_monitor.update_manifest import (
    ArtifactDescriptor,
    UpdateCheckResult,
    UpdateManifest,
    UpdateStatus,
)


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


class TimeoutAwareClient(SuccessfulClient):
    created_with = []

    def __init__(self, key, timeout):
        super().__init__(key)
        self.created_with.append(timeout)


@contextmanager
def running_server(client_factory=SuccessfulClient, **server_options):
    server = create_server(client_factory=client_factory, **server_options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, path, headers=None, *, method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, body


def config_service(tmp_path):
    return ConfigService(
        AppPaths(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "log",
        )
    )


def test_server_binds_only_to_ipv4_loopback():
    with running_server() as server:
        assert server.server_address[0] == "127.0.0.1"


def test_catalog_has_version_no_store_and_browser_security_headers():
    with running_server() as server:
        status, headers, body = request(server, "/api/catalog")
    assert status == 200
    assert json.loads(body)["version"] == __version__
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Request-ID"]


def test_versioned_health_and_catalog_contracts_are_available():
    with running_server() as server:
        status, headers, body = request(server, "/api/v1/health")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert json.loads(body) == {"version": __version__, "api_version": "v1", "ready": True}

        status, _, body = request(server, "/api/v1/catalog")
        assert status == 200
        assert json.loads(body)["schema_version"] == 1


@pytest.mark.parametrize("locale", ["en", "zh-TW"])
def test_locale_endpoint_serves_only_allowlisted_json_resources(locale):
    with running_server() as server:
        status, headers, body = request(server, f"/api/v1/locales/{locale}")

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert isinstance(json.loads(body), dict)


@pytest.mark.parametrize(
    ("locale_path", "status", "code"),
    [
        ("fr", 404, "locale_not_found"),
        ("..%2f..%2fREADME.md", 403, "forbidden_path"),
        ("en%2f..%2fzh-TW", 403, "forbidden_path"),
    ],
)
def test_locale_endpoint_rejects_unknown_names_and_path_traversal(
    locale_path, status, code
):
    with running_server() as server:
        actual_status, _, body = request(
            server, f"/api/v1/locales/{locale_path}"
        )

    assert actual_status == status
    assert json.loads(body)["error"]["code"] == code


def test_config_get_and_put_use_effective_config_and_atomic_service(tmp_path):
    service = config_service(tmp_path)
    with running_server(config_service=service) as server:
        status, headers, body = request(server, "/api/v1/config")
        initial = json.loads(body)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert initial["config"] == config_to_dict(AppConfig())
        assert initial["defaults"] == config_to_dict(AppConfig())
        assert initial["config_path"] == str(service.paths.config_file)
        assert initial["restart_required"] is False

        default_channel = AppConfig().updates.channel
        changed_channel = "stable" if default_channel == "beta" else "beta"
        changed = config_to_dict(
            AppConfig(
                network=NetworkSettings(request_timeout_seconds=90),
                updates=UpdateSettings(channel=changed_channel, check_on_start=False),
            )
        )
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(changed),
        )
        saved = json.loads(body)
        assert status == 200
        assert saved["config"] == changed
        assert saved["defaults"] == config_to_dict(AppConfig())
        assert saved["restart_required"] is True
        assert saved["restart_required_fields"] == ["updates.channel"]

        status, _, body = request(server, "/api/v1/config")
        assert status == 200
        assert json.loads(body)["config"] == changed

    assert json.loads(service.paths.config_file.read_text(encoding="utf-8")) == changed


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"not-json", "invalid_json"),
        (
            json.dumps(
                {"schema_version": 1, "network": {"request_timeout_seconds": "slow"}}
            ),
            "invalid_config",
        ),
        (json.dumps({"schema_version": 1, "admin_api_key": "top-secret"}), "invalid_config"),
    ],
)
def test_config_put_rejects_invalid_json_unknown_and_sensitive_fields(
    tmp_path, body, expected_code
):
    service = config_service(tmp_path)
    with running_server(config_service=service) as server:
        status, _, response_body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=body,
        )

    payload = json.loads(response_body)
    assert status == 400
    assert payload["error"]["code"] == expected_code
    assert "top-secret" not in response_body.decode("utf-8")
    assert not service.paths.config_file.exists()


def test_config_put_preserves_safe_unknown_fields_across_get_put_get(tmp_path):
    service = config_service(tmp_path)
    document = {**config_to_dict(AppConfig()), "future_optional": {"enabled": True}}
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(document),
        )
        assert status == 200
        assert json.loads(body)["config"]["future_optional"] == {"enabled": True}

        status, _, body = request(server, "/api/v1/config")
        assert status == 200
        returned = json.loads(body)["config"]

        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(returned),
        )
        assert status == 200
        assert json.loads(body)["config"]["future_optional"] == {"enabled": True}


def test_config_put_rejects_secret_like_unknown_field_name_without_echo(tmp_path):
    service = config_service(tmp_path)
    secret_field = "sk-" + "admin-field-secret-12345678"
    document = {**config_to_dict(AppConfig()), secret_field: True}
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(document),
        )

    assert status == 400
    assert json.loads(body)["error"]["code"] == "invalid_config"
    assert secret_field.encode() not in body
    assert not service.paths.config_file.exists()


def test_config_put_enforces_content_type_and_body_size_before_parsing(tmp_path):
    service = config_service(tmp_path)
    oversized = b"{" + b" " * (64 * 1024)
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=oversized,
        )
        assert status == 413
        assert json.loads(body)["error"]["code"] == "request_too_large"

        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "text/plain"},
            method="PUT",
            body=b"{}",
        )
        assert status == 415
        assert json.loads(body)["error"]["code"] == "unsupported_media_type"


def test_cross_site_config_put_is_rejected_without_writing(tmp_path):
    service = config_service(tmp_path)
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            method="PUT",
            body=json.dumps(config_to_dict(AppConfig())),
        )

    assert status == 403
    assert json.loads(body)["error"]["code"] == "cross_site_request"
    assert not service.paths.config_file.exists()


def test_config_write_failure_uses_stable_error_and_keeps_active_config(
    monkeypatch, tmp_path
):
    service = config_service(tmp_path)
    changed = config_to_dict(
        AppConfig(network=NetworkSettings(request_timeout_seconds=90))
    )

    def fail_save(_config):
        raise ConfigWriteError("private operating system detail")

    monkeypatch.setattr(service, "save", fail_save)
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(changed),
        )
        read_status, _, read_body = request(server, "/api/v1/config")

    assert status == 500
    assert json.loads(body)["error"]["code"] == "config_write_failed"
    assert "private operating system detail" not in body.decode("utf-8")
    assert read_status == 200
    assert json.loads(read_body)["config"] == config_to_dict(AppConfig())


def test_startup_setting_is_rejected_when_platform_capability_is_unavailable(tmp_path):
    service = config_service(tmp_path)
    changed = config_to_dict(AppConfig(startup=StartupSettings(enabled=True)))
    with running_server(config_service=service) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(changed),
        )

    payload = json.loads(body)
    assert status == 409
    assert payload["error"]["code"] == "capability_unavailable"
    assert payload["error"]["params"] == {"capability": "startup"}
    assert not service.paths.config_file.exists()


def test_startup_setting_is_applied_immediately_without_requiring_restart(tmp_path):
    service = config_service(tmp_path)
    startup = InMemoryStartupAdapter()
    runtime = RuntimeServices(
        config_service=service,
        startup_adapter=startup,
    )
    changed = config_to_dict(AppConfig(startup=StartupSettings(enabled=True)))

    with running_server(runtime_services=runtime) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(changed),
        )

    payload = json.loads(body)
    assert status == 200
    assert startup.is_enabled() is True
    assert payload["restart_required"] is False
    assert payload["restart_required_fields"] == []
    assert payload["applied_fields"] == ["startup.enabled"]


def test_startup_adapter_rolls_back_when_config_write_fails(monkeypatch, tmp_path):
    service = config_service(tmp_path)
    startup = InMemoryStartupAdapter()
    runtime = RuntimeServices(config_service=service, startup_adapter=startup)
    changed = config_to_dict(AppConfig(startup=StartupSettings(enabled=True)))

    def fail_save(_config):
        raise ConfigWriteError("simulated")

    monkeypatch.setattr(service, "save", fail_save)
    with running_server(runtime_services=runtime) as server:
        status, _, body = request(
            server,
            "/api/v1/config",
            {"Content-Type": "application/json"},
            method="PUT",
            body=json.dumps(changed),
        )

    assert status == 500
    assert json.loads(body)["error"]["code"] == "config_write_failed"
    assert startup.is_enabled() is False
    assert runtime.config.startup.enabled is False


class StaticUpdateCheckService:
    def check(self):
        manifest = UpdateManifest(
            schema_version=1,
            channel="stable",
            version=SemVer.parse("0.2.1"),
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            minimum_updater_version=SemVer.parse("0.2.0"),
            artifacts=(
                ArtifactDescriptor(
                    url="https://downloads.example/release.exe",
                    size=123,
                    sha256="0" * 64,
                    os="windows",
                    arch="x86_64",
                    format="portable",
                ),
            ),
            release_notes_url="https://releases.example/v0.2.1",
            signature=b"s" * 64,
            key_id="release-key-1",
            signing_payload=b"{}",
            critical=False,
        )
        return UpdateCheckResult(UpdateStatus.AVAILABLE, manifest)


def test_update_check_is_explicitly_unavailable_without_injected_capability(tmp_path):
    with running_server(config_service=config_service(tmp_path)) as server:
        for method in ("GET", "POST"):
            status, _, body = request(
                server, "/api/v1/update/check", method=method
            )
            assert status == 503
            payload = json.loads(body)
            assert payload["error"]["code"] == "capability_unavailable"
            assert payload["error"]["params"] == {"capability": "update_check"}


def test_injected_update_check_returns_only_safe_read_only_metadata(tmp_path):
    with running_server(
        config_service=config_service(tmp_path),
        update_checker=StaticUpdateCheckService(),
    ) as server:
        status, _, body = request(server, "/api/v1/update/check", method="POST")

    payload = json.loads(body)
    assert status == 200
    assert payload["status"] == "available"
    assert payload["available"] is True
    assert payload["update"] == {
        "version": "0.2.1",
        "channel": "stable",
        "published_at": "2026-08-01T00:00:00Z",
        "release_notes_url": "https://releases.example/v0.2.1",
        "critical": False,
        "artifacts": [
            {"os": "windows", "arch": "x86_64", "format": "portable", "size": 123}
        ],
    }
    assert "sha256" not in body.decode("utf-8")
    assert "downloads.example" not in body.decode("utf-8")


class FakeProductUpdateRuntime:
    def __init__(self):
        self.version = "0.2.1"
        self.state = "available"
        self.actions = []

    def check(self, *, prepare=True):
        return StaticUpdateCheckService().check()

    def status(self):
        return {
            "state": self.state,
            "operation": None,
            "version": self.version,
            "channel": "stable",
            "critical": False,
            "release_notes_url": "https://releases.example/v0.2.1",
            "progress": {
                "phase": self.state,
                "completed_bytes": 0,
                "total_bytes": 123,
                "percent": 0,
            },
            "last_error_code": None,
            "recovery_required": False,
            "can_consent_download": self.state == "available",
            "can_download": self.state == "download-consented",
            "can_consent_install": self.state == "staged",
            "can_install": self.state == "install-consented",
        }

    def consent_download(self, *, version, confirm):
        assert version == self.version and confirm is True
        self.actions.append("consent-download")
        self.state = "download-consented"
        return self.status()

    def start_download(self):
        self.actions.append("download")
        self.state = "staged"
        return self.status()

    def consent_install(self, *, version, confirm):
        assert version == self.version and confirm is True
        self.actions.append("consent-install")
        self.state = "install-consented"
        return self.status()

    def start_install(self):
        self.actions.append("install")
        self.state = "committed"
        return self.status()

    def resume(self):
        self.actions.append("resume")
        return self.status()


def test_product_update_api_exposes_status_and_version_bound_actions(tmp_path):
    updater = FakeProductUpdateRuntime()
    runtime = RuntimeServices(
        config_service=config_service(tmp_path), update_runtime=updater
    )
    json_headers = {"Content-Type": "application/json"}
    with running_server(runtime_services=runtime) as server:
        status, _, body = request(server, "/api/v1/update/status")
        assert status == 200
        assert json.loads(body)["state"] == "available"

        for path, document, expected in (
            ("consent-download", {"version": "0.2.1", "confirm": True}, 200),
            ("download", {}, 202),
            ("consent-install", {"version": "0.2.1", "confirm": True}, 200),
            ("install", {}, 202),
            ("resume", {}, 202),
        ):
            status, headers, body = request(
                server,
                f"/api/v1/update/{path}",
                json_headers,
                method="POST",
                body=json.dumps(document),
            )
            assert status == expected
            payload = json.loads(body)
            assert "request_id" in payload
            assert "sha256" not in body.decode("utf-8")
            assert headers["Cache-Control"] == "no-store"

    assert updater.actions == [
        "consent-download", "download", "consent-install", "install", "resume"
    ]


def test_product_update_api_rejects_unknown_fields_and_is_explicitly_unavailable(tmp_path):
    with running_server(config_service=config_service(tmp_path)) as server:
        status, _, body = request(server, "/api/v1/update/status")
        assert status == 503
        assert json.loads(body)["error"]["params"] == {"capability": "update_runtime"}

    updater = FakeProductUpdateRuntime()
    runtime = RuntimeServices(
        config_service=config_service(tmp_path / "configured"), update_runtime=updater
    )
    with running_server(runtime_services=runtime) as server:
        status, _, body = request(
            server,
            "/api/v1/update/download",
            {"Content-Type": "application/json"},
            method="POST",
            body=json.dumps({"url": "https://evil.invalid/payload"}),
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "unknown_request_field"
        assert updater.actions == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://attacker.invalid"},
        {"Origin": "http://127.0.0.1:1"},
    ],
)
def test_api_rejects_cross_site_or_wrong_origin_requests(headers):
    with running_server() as server:
        status, _, body = request(server, "/api/v1/health", headers)

    assert status == 403
    assert json.loads(body)["error"]["code"] in {"cross_site_request", "invalid_origin"}


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


def test_default_client_factory_uses_the_effective_request_timeout(
    monkeypatch, tmp_path
):
    service = config_service(tmp_path)
    service.save(AppConfig(network=NetworkSettings(request_timeout_seconds=90)))
    TimeoutAwareClient.created_with.clear()
    monkeypatch.setattr("quota_monitor.server.OpenAIAdminClient", TimeoutAwareClient)

    with running_server(None, config_service=service) as server:
        status, _, _ = request(
            server,
            "/api/v1/data",
            {"X-Admin-Key": ADMIN_KEY},
        )

    assert status == 200
    assert TimeoutAwareClient.created_with == [90]


def test_page_source_does_not_use_browser_persistent_storage():
    source = resource_path("web", "js", "app.js").read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "indexedDB" not in source
