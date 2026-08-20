from pathlib import Path

import pytest

from quota_monitor.platform_adapters import (
    AdapterUnavailableError,
    CredentialNotFoundError,
    CredentialStore,
    InMemoryCredentialStore,
    InMemoryInstanceLock,
    InMemoryNotificationAdapter,
    InMemoryStartupAdapter,
    InMemoryTrayAdapter,
    InMemoryUpdaterAdapter,
    MemoryLockRegistry,
    NotificationMessage,
    PlatformFamily,
    TrayState,
    UnavailableCredentialStore,
    UpdateInfo,
    create_platform_services,
    detect_platform_family,
    resolve_platform_paths,
)


def test_default_factory_exposes_honest_fail_closed_capabilities(tmp_path):
    services = create_platform_services(platform_name="linux", environ={}, home=tmp_path)

    assert services.family is PlatformFamily.LINUX
    assert services.capabilities.credential_store is False
    assert services.capabilities.tray is False
    assert services.capabilities.notifications is False
    assert services.capabilities.instance_lock is False
    assert services.instance_lock.acquire() is False
    assert services.notifications.send(NotificationMessage("title", "body")) is False
    assert services.updater.check("0.1.0").available is False

    with pytest.raises(AdapterUnavailableError):
        services.credential_store.put("prof_" + "a" * 32, "not-written-anywhere")


def test_unavailable_credential_backend_never_leaks_supplied_secret():
    secret = "sensitive-value-for-test"
    backend = UnavailableCredentialStore()

    with pytest.raises(AdapterUnavailableError) as caught:
        backend.put("prof_" + "a" * 32, secret)

    assert secret not in str(caught.value)
    assert secret not in repr(backend)


def test_in_memory_credential_store_returns_only_opaque_references():
    store = InMemoryCredentialStore()
    account = "prof_" + "a" * 32

    reference = store.put(account, "first-secret")
    replacement = store.put(account, "replacement-secret")

    assert isinstance(store, CredentialStore)
    assert reference == replacement
    assert store.get(reference) == "replacement-secret"
    assert "replacement-secret" not in repr(store)
    assert store.delete(reference) is True
    with pytest.raises(CredentialNotFoundError):
        store.get(reference)


def test_memory_instance_locks_share_a_registry_and_release_cleanly():
    registry = MemoryLockRegistry()
    first = InMemoryInstanceLock("quota-monitor", registry)
    second = InMemoryInstanceLock("quota-monitor", registry)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_memory_adapters_support_contract_level_state_transitions():
    tray = InMemoryTrayAdapter()
    notifications = InMemoryNotificationAdapter()
    startup = InMemoryStartupAdapter()
    updater = InMemoryUpdaterAdapter(
        UpdateInfo("0.4.0", "https://updates.invalid/v0.4.0", "a" * 64)
    )

    assert tray.start() is True
    tray.set_state(TrayState.SYNCING)
    assert tray.state is TrayState.SYNCING
    tray.shutdown()
    assert tray.running is False

    message = NotificationMessage("notification.quota_title", "notification.quota_body")
    assert notifications.send(message) is True
    assert notifications.messages == [message]

    assert startup.enable() is True
    assert startup.is_enabled() is True
    assert startup.disable() is True
    assert startup.is_enabled() is False

    assert updater.check("0.3.1").update.version == "0.4.0"


def test_notification_contract_rejects_credentials_and_external_deep_links():
    with pytest.raises(ValueError, match="credentials"):
        NotificationMessage(
            "notification.title",
            "notification.body",
            {"value": "sk-admin-" + "x" * 12},
        )
    with pytest.raises(ValueError, match="local application path"):
        NotificationMessage(
            "notification.title",
            "notification.body",
            deep_link="https://example.invalid/dashboard",
        )


@pytest.mark.parametrize(
    ("platform_name", "family"),
    [
        ("win32", PlatformFamily.WINDOWS),
        ("darwin", PlatformFamily.MACOS),
        ("linux", PlatformFamily.LINUX),
        ("freebsd", PlatformFamily.UNKNOWN),
    ],
)
def test_platform_detection_is_centralized(platform_name, family):
    assert detect_platform_family(platform_name) is family


def test_linux_paths_honor_xdg_and_do_not_depend_on_cwd(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "configuration"),
        "XDG_DATA_HOME": str(tmp_path / "records"),
        "XDG_CACHE_HOME": str(tmp_path / "cached"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    paths = resolve_platform_paths(PlatformFamily.LINUX, environ=environment, home=tmp_path)

    assert paths.config_dir == tmp_path / "configuration" / "OpenAI-Free-Credit-Tracker"
    assert paths.data_dir == tmp_path / "records" / "OpenAI-Free-Credit-Tracker"
    assert paths.cache_dir == tmp_path / "cached" / "OpenAI-Free-Credit-Tracker"
    assert paths.log_dir == tmp_path / "state" / "OpenAI-Free-Credit-Tracker" / "log"
    assert not paths.config_dir.exists()
    paths.ensure_directories()
    assert all(
        path.is_dir()
        for path in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir)
    )


def test_path_component_cannot_escape_native_roots(tmp_path):
    with pytest.raises(ValueError):
        resolve_platform_paths(
            PlatformFamily.WINDOWS,
            app_directory="../outside",
            environ={},
            home=Path(tmp_path),
        )
