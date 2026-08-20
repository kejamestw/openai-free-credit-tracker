import json
import sys
from pathlib import Path

import pytest

from quota_monitor.i18n import LocaleCatalog
from quota_monitor.platform_adapters import (
    DesktopNotifierBackend,
    DesktopTrayAdapter,
    FileActivationChannel,
    FileInstanceLock,
    LocalizedNotificationAdapter,
    NotificationMessage,
    NotificationPermission,
    NotificationPermissionDenied,
    PlatformFamily,
    TrayAction,
    TrayActions,
    TrayState,
    create_desktop_platform_services,
    desktop_session_available,
)


class FakeTrayBackend:
    available = True

    def __init__(self):
        self.entries = ()
        self.states = []
        self.running = False
        self.refreshes = 0

    def start(self, entries, state):
        self.entries = tuple(entries)
        self.states.append(state)
        self.running = True
        return True

    def set_state(self, state):
        self.states.append(state)

    def refresh_menu(self):
        self.refreshes += 1

    def shutdown(self):
        self.running = False


class FakeNotificationBackend:
    available = True

    def __init__(self, *, denied=False):
        self.denied = denied
        self.calls = []

    def request_permission(self):
        return NotificationPermission.DENIED if self.denied else NotificationPermission.GRANTED

    def send(self, title, body, *, on_clicked=None):
        self.calls.append((title, body, on_clicked))
        return True

    def shutdown(self):
        self.available = False


class FakeActivationRunner:
    def __init__(self):
        self.items = {}
        self.writes = 0

    def atomic_write(self, path, content):
        self.items[path] = content
        self.writes += 1

    def read(self, path):
        return self.items.get(path)

    def remove(self, path):
        self.items.pop(path, None)


def locale(name="en"):
    return LocaleCatalog.from_directory(name)


def test_tray_exposes_all_actions_and_dynamic_pause_startup_state():
    backend = FakeTrayBackend()
    tray = DesktopTrayAdapter(backend, locale())
    called = []
    state = {"paused": False, "startup": False}
    tray.bind(
        TrayActions(
            open_dashboard=lambda: called.append("open"),
            sync_now=lambda: called.append("sync"),
            toggle_pause=lambda: called.append("pause"),
            toggle_startup=lambda: called.append("startup"),
            show_about=lambda: called.append("about"),
            exit=lambda: called.append("exit"),
            is_paused=lambda: state["paused"],
            startup_enabled=lambda: state["startup"],
        )
    )

    assert tray.start() is True
    assert {entry.action for entry in backend.entries} == set(TrayAction)
    for entry in backend.entries:
        entry.invoke()
    assert called == ["open", "sync", "pause", "startup", "about", "exit"]

    pause = next(item for item in backend.entries if item.action is TrayAction.TOGGLE_PAUSE)
    startup = next(item for item in backend.entries if item.action is TrayAction.TOGGLE_STARTUP)
    assert pause.label() == "Pause monitoring"
    assert startup.checked() is False
    state.update(paused=True, startup=True)
    assert pause.label() == "Resume monitoring"
    assert startup.checked() is True

    tray.set_state(TrayState.ERROR)
    tray.shutdown()
    assert backend.states[-1] is TrayState.ERROR
    assert backend.running is False


def test_localized_notification_validates_click_route_and_denial_is_sticky():
    opened = []
    backend = FakeNotificationBackend()
    notifications = LocalizedNotificationAdapter(
        backend,
        locale("zh-TW"),
        open_deep_link=opened.append,
    )
    profile_id = "prof_" + "a" * 32
    message = NotificationMessage(
        "notification.quota_title",
        "notification.quota_body",
        {"profile": "Profile ••••aaaa", "percent": "80"},
        deep_link=(
            f"/dashboard?profile_id={profile_id}&view=alerts&utc_day=2026-08-09"
            "&project_key=project-0123456789abcdef01234567"
        ),
    )

    assert notifications.send(message) is True
    title, body, click = backend.calls[0]
    assert title == "已達額度門檻"
    assert "80%" in body
    click()
    assert opened == [
        f"/dashboard?profile_id={profile_id}&project_key=project-0123456789abcdef01234567"
        "&utc_day=2026-08-09&view=alerts"
    ]

    denied_backend = FakeNotificationBackend(denied=True)
    denied = LocalizedNotificationAdapter(
        denied_backend,
        locale(),
        open_deep_link=lambda _path: None,
    )
    assert denied.send(message) is False
    assert denied.permission is NotificationPermission.DENIED
    assert denied.send(message) is False
    assert denied_backend.calls == []


def test_desktop_notifier_backend_keeps_async_loop_until_shutdown():
    class FakeNotifier:
        def __init__(self):
            self.calls = []

        async def request_authorisation(self):
            return True

        async def send(self, **kwargs):
            self.calls.append(kwargs)
            return "notification-id"

    notifier = FakeNotifier()
    backend = DesktopNotifierBackend(notifier)

    assert backend.request_permission() is NotificationPermission.GRANTED
    assert backend.send("Title", "Body", on_clicked=lambda: None) is True
    assert notifier.calls[0]["title"] == "Title"
    backend.shutdown()
    assert backend.available is False


def test_file_activation_channel_has_bounded_action_only_payload(tmp_path):
    runner = FakeActivationRunner()
    path = (tmp_path / "activate.json").resolve()
    channel = FileActivationChannel(path, runner)

    assert channel.request() is True
    payload = json.loads(runner.items[path])
    assert payload.keys() == {"action", "nonce"}
    assert payload["action"] == "open_dashboard"
    assert channel.consume() is True
    assert channel.consume() is False


def test_desktop_factory_autoselects_real_file_lock_and_fails_closed_unknown_platform(tmp_path):
    services = create_desktop_platform_services(
        executable=(tmp_path / "quota-monitor").resolve(),
        locale=locale(),
        open_deep_link=lambda _path: None,
        platform_name="freebsd",
        environ={},
        home=tmp_path,
    )

    assert isinstance(services.instance_lock, FileInstanceLock)
    assert services.capabilities.instance_lock is True
    assert services.capabilities.credential_store is False
    assert services.capabilities.tray is False
    assert services.capabilities.notifications is False


def test_linux_headless_or_missing_dbus_is_not_a_desktop_session():
    assert desktop_session_available(
        PlatformFamily.LINUX,
        environ={},
        host_platform="linux",
    ) is False
    assert desktop_session_available(
        PlatformFamily.LINUX,
        environ={"DISPLAY": ":0"},
        host_platform="linux",
    ) is False
    assert desktop_session_available(
        PlatformFamily.LINUX,
        environ={"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus"},
        host_platform="linux",
    ) is True


@pytest.mark.native_desktop
@pytest.mark.skipif(
    not bool(__import__("os").environ.get("RUN_NATIVE_DESKTOP_TESTS")),
    reason="set RUN_NATIVE_DESKTOP_TESTS=1 on an isolated desktop CI runner",
)
def test_native_desktop_factory_reports_current_host_capabilities(tmp_path):
    services = create_desktop_platform_services(
        executable=Path(sys.executable).resolve(),
        locale=locale(),
        open_deep_link=lambda _path: None,
        home=tmp_path,
    )
    assert services.family in {
        PlatformFamily.WINDOWS,
        PlatformFamily.MACOS,
        PlatformFamily.LINUX,
    }
    assert services.instance_lock.available is True
