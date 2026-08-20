import threading
from dataclasses import replace
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from quota_monitor.alerts import AlertRule
from quota_monitor.config_service import (
    AppConfig,
    MonitoringSettings,
    ProfilesSettings,
    UISettings,
)
from quota_monitor.database import DatabaseService
from quota_monitor.desktop_integration import (
    LoopbackDesktopServer,
    ProductionProfileSyncer,
    build_desktop_composition,
)
from quota_monitor.model_catalog import load_catalog
from quota_monitor.openai_client import OpenAIClientError
from quota_monitor.operations_cli import RUNTIME_LOCK_FILENAME
from quota_monitor.platform_adapters import (
    FileInstanceLock,
    InMemoryCredentialStore,
    InMemoryInstanceLock,
    InMemoryNotificationAdapter,
    InMemoryStartupAdapter,
    InMemoryTrayAdapter,
    InMemoryUpdaterAdapter,
    create_platform_services,
)
from quota_monitor.platform_adapters.deep_links import DeepLinkValidationError
from quota_monitor.platform_paths import AppPaths
from quota_monitor.profiles import ProfileId
from quota_monitor.runtime import RuntimeServices
from quota_monitor.scheduler import RunStatus
from quota_monitor.server import create_server
from quota_monitor.upstream_adapter import ProjectKeyDeriver


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
START = int(datetime(2026, 8, 9, tzinfo=timezone.utc).timestamp())
END = int(NOW.timestamp())
PROFILE_ID = ProfileId("prof_" + "a" * 32)
ADMIN_KEY = "sk-" + "admin-" + "super-secret-material"
RAW_PROJECT_ID = "proj_private_do_not_expose"


def make_paths(tmp_path):
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )


class FixedClock:
    def monotonic(self):
        return 123.0

    def utc_now(self):
        return NOW


class FakeLoopbackBackend:
    server_address = ("127.0.0.1", 24680)

    def __init__(self):
        self.serving = threading.Event()
        self.stop = threading.Event()
        self.closed = False

    def serve_forever(self, poll_interval=0.5):
        self.serving.set()
        self.stop.wait(2)

    def shutdown(self):
        self.stop.set()

    def server_close(self):
        self.closed = True


def test_loopback_wrapper_is_nonblocking_validates_routes_and_closes_cleanly():
    backend = FakeLoopbackBackend()
    opened = []
    server = LoopbackDesktopServer(lambda: backend, browser_open=opened.append)

    server.start()

    assert backend.serving.wait(1)
    assert server.base_url == "http://127.0.0.1:24680"
    server.open_dashboard("/dashboard?view=alerts")
    assert opened == ["http://127.0.0.1:24680/dashboard?view=alerts"]
    with pytest.raises(DeepLinkValidationError):
        server.open_dashboard("https://example.com/steal")

    server.shutdown()
    assert backend.closed is True
    assert server.base_url is None


def test_real_loopback_server_serves_validated_dashboard_spa_route(tmp_path):
    paths = make_paths(tmp_path)
    runtime = RuntimeServices(paths=paths, initial_config=AppConfig())
    server = LoopbackDesktopServer(
        lambda: create_server(runtime_services=runtime),
        browser_open=lambda _url: None,
    )
    server.start()
    try:
        deep_link = (
            f"{server.base_url}/dashboard?profile_id={PROFILE_ID.value}"
            "&project_key=project-0123456789abcdef01234567"
            "&utc_day=2026-08-09&view=alerts"
        )
        with urlopen(deep_link, timeout=5) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
            assert b'<main id="mainContent"' in body

        with pytest.raises(HTTPError) as captured:
            urlopen(f"{server.base_url}/dashboard?view=not-allowed", timeout=5)
        assert captured.value.code == 400
        assert b"invalid_navigation_target" in captured.value.read()

        with pytest.raises(HTTPError) as encoded:
            urlopen(f"{server.base_url}/dash%62oard?view=not-allowed", timeout=5)
        assert encoded.value.code == 400
        assert b"invalid_navigation_target" in encoded.value.read()
    finally:
        server.shutdown()


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def usage_response():
    return {
        "data": [
            {
                "start_time": START,
                "end_time": END,
                "results": [
                    {
                        "project_id": RAW_PROJECT_ID,
                        "model": "gpt-5.6-terra",
                        "service_tier": "incentivized-tier",
                        "input_tokens": 1_000_000,
                        "input_cached_tokens": 0,
                        "output_tokens": 250_000,
                        "num_model_requests": 10,
                    }
                ],
            }
        ],
        "next_page": None,
    }


def make_runtime(tmp_path, response):
    paths = make_paths(tmp_path)
    paths.ensure_directories()
    database = DatabaseService(paths.data_dir / "history.sqlite3")
    credentials = InMemoryCredentialStore()
    reference = credentials.put(PROFILE_ID.value, ADMIN_KEY)
    clients = []

    def client_factory(key, timeout):
        client = FakeClient(response)
        clients.append((key, timeout, client))
        return client

    runtime = RuntimeServices(
        paths=paths,
        initial_config=AppConfig(),
        database=database,
        credential_store=credentials,
        admin_client_factory=client_factory,
        project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
        catalog=load_catalog(),
    )
    runtime.profile_service.create("Primary", reference)
    return runtime, clients


def test_production_syncer_uses_injected_secret_and_builds_safe_quota_observations(
    tmp_path,
):
    runtime, clients = make_runtime(tmp_path, usage_response())
    project_key = runtime.project_keys.derive(RAW_PROJECT_ID)
    missing_project = runtime.project_keys.derive("proj_missing")
    runtime.alert_state.save_rule(
        AlertRule(
            "rule-selected",
            PROFILE_ID.value,
            "mini",
            25,
            project_key=missing_project,
        )
    )

    result = ProductionProfileSyncer(runtime, clock=FixedClock()).sync(
        PROFILE_ID.value,
        ADMIN_KEY,
    )

    assert result.status is RunStatus.SUCCESS
    assert clients[0][:2] == (ADMIN_KEY, 45)
    assert clients[0][2].calls[0][1]["start_time"] == START
    assert clients[0][2].calls[0][1]["end_time"] == END
    by_scope = {
        (item.group_id, item.project_key): item for item in result.observations
    }
    assert by_scope[("mini", "all")].percent == 50
    assert by_scope[("mini", project_key)].percent == 50
    assert by_scope[("mini", missing_project)].percent == 0
    assert all(item.fresh and item.observed_at == NOW for item in result.observations)
    assert RAW_PROJECT_ID not in repr(result)
    assert ADMIN_KEY not in repr(result)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OpenAIClientError("authentication_failed", "safe", 401, False),
            RunStatus.AUTH_ERROR,
        ),
        (
            OpenAIClientError("rate_limited", "safe", 429, True),
            RunStatus.RETRYABLE_ERROR,
        ),
    ],
)
def test_production_syncer_maps_sanitized_upstream_statuses(tmp_path, error, expected):
    runtime, _clients = make_runtime(tmp_path, error)

    result = ProductionProfileSyncer(runtime, clock=FixedClock()).sync(
        PROFILE_ID.value,
        ADMIN_KEY,
    )

    assert result.status is expected
    assert result.error_code == error.code
    assert ADMIN_KEY not in repr(result)


@pytest.mark.parametrize(
    ("response", "expected", "error_code"),
    [
        (usage_response(), RunStatus.PARTIAL, "observation_build_failed"),
        (
            OpenAIClientError("authentication_failed", "safe", 401, False),
            RunStatus.AUTH_ERROR,
            "authentication_failed",
        ),
        (
            OpenAIClientError("rate_limited", "safe", 429, True),
            RunStatus.RETRYABLE_ERROR,
            "rate_limited",
        ),
    ],
)
def test_observation_failure_preserves_more_important_sync_status(
    tmp_path, monkeypatch, response, expected, error_code
):
    runtime, _clients = make_runtime(tmp_path, response)
    syncer = ProductionProfileSyncer(runtime, clock=FixedClock())

    def fail_observations(*_args, **_kwargs):
        raise RuntimeError("simulated local observation failure")

    monkeypatch.setattr(syncer, "_build_observations", fail_observations)

    result = syncer.sync(PROFILE_ID.value, ADMIN_KEY)

    assert result.status is expected
    assert result.error_code == error_code
    assert result.observations == ()
    assert ADMIN_KEY not in repr(result)


def test_composition_reuses_data_credentials_shared_lock_and_config(tmp_path):
    paths = make_paths(tmp_path)
    database = DatabaseService(paths.data_dir / "history.sqlite3")
    credentials = InMemoryCredentialStore()
    config = AppConfig(
        ui=UISettings(language="en", open_browser_on_start=True),
        monitoring=MonitoringSettings(
            enabled=True,
            interval_seconds=600,
            freshness_threshold_seconds=1200,
        ),
        profiles=ProfilesSettings(active_profile_id=None),
    )
    runtime = RuntimeServices(
        paths=paths,
        initial_config=config,
        database=database,
        credential_store=credentials,
        admin_client_factory=lambda _key, _timeout: FakeClient(usage_response()),
        project_keys=ProjectKeyDeriver(b"0123456789abcdef"),
        catalog=load_catalog(),
    )
    captured = {}

    def platform_factory(**kwargs):
        captured.update(kwargs)
        platform = create_platform_services(
            platform_name="win32",
            credential_store=kwargs["credential_store"],
            tray=InMemoryTrayAdapter(),
            notifications=InMemoryNotificationAdapter(),
            startup=InMemoryStartupAdapter(),
            instance_lock=kwargs["instance_lock"],
            updater=InMemoryUpdaterAdapter(),
        )
        return replace(platform, paths=kwargs["paths"])

    composition = build_desktop_composition(
        paths,
        no_browser=True,
        data_runtime_factory=lambda received: (
            runtime if received is paths else (_ for _ in ()).throw(AssertionError())
        ),
        http_server_factory=lambda _runtime: FakeLoopbackBackend(),
        platform_services_factory=platform_factory,
        clock=FixedClock(),
    )

    assert composition.data_runtime is runtime
    assert composition.platform.credential_store is credentials
    assert captured["credential_store"] is credentials
    assert captured["paths"] is paths
    assert isinstance(captured["instance_lock"], FileInstanceLock)
    assert captured["instance_lock"].path == (
        paths.data_dir / RUNTIME_LOCK_FILENAME
    ).resolve()
    assert runtime.startup_adapter is composition.platform.startup
    assert runtime.notification_adapter is composition.platform.notifications
    assert composition.scheduler._interval_seconds == 600
    assert composition.desktop._monitoring_enabled is True
    assert composition.desktop._open_dashboard_on_start is False
    assert captured["startup_arguments"][-1] == "--background"


def test_composition_rejects_a_platform_factory_that_replaces_the_shared_lock(tmp_path):
    paths = make_paths(tmp_path)
    runtime, _clients = make_runtime(tmp_path, usage_response())

    def platform_factory(**kwargs):
        platform = create_platform_services(
            platform_name="win32",
            credential_store=kwargs["credential_store"],
            tray=InMemoryTrayAdapter(),
            notifications=InMemoryNotificationAdapter(),
            startup=InMemoryStartupAdapter(),
            instance_lock=InMemoryInstanceLock("different-runtime-lock"),
            updater=InMemoryUpdaterAdapter(),
        )
        return replace(platform, paths=kwargs["paths"])

    with pytest.raises(RuntimeError, match="second runtime lock"):
        build_desktop_composition(
            paths,
            data_runtime_factory=lambda _paths: runtime,
            http_server_factory=lambda _runtime: FakeLoopbackBackend(),
            platform_services_factory=platform_factory,
            clock=FixedClock(),
        )
