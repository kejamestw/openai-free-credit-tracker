import threading
from datetime import date, datetime, timezone

from quota_monitor.alert_storage import SQLiteAlertState
from quota_monitor.alerts import AlertRule, UsageObservation
from quota_monitor.database import DatabaseService
from quota_monitor.desktop_runtime import (
    DesktopRuntime,
    DesktopStartMode,
    PersistentAlertDispatcher,
    ProfileSchedulerCoordinator,
    ProfileSyncResult,
)
from quota_monitor.i18n import LocaleCatalog
from quota_monitor.platform_adapters import (
    InMemoryCredentialStore,
    InMemoryInstanceLock,
    InMemoryNotificationAdapter,
    InMemoryStartupAdapter,
    InMemoryUpdaterAdapter,
    MemoryLockRegistry,
    PlatformFamily,
    TrayState,
    create_platform_services,
)
from quota_monitor.platform_adapters.desktop import DesktopTrayAdapter, UnavailableTrayBackend
from quota_monitor.platform_adapters.desktop import (
    LocalizedNotificationAdapter,
    NotificationPermission,
)
from quota_monitor.profiles import (
    InMemoryProfileRepository,
    ProfileId,
    ProfileService,
    SQLiteProfileRepository,
)
from quota_monitor.scheduler import RunStatus, SchedulerState


PROFILE_A = "prof_" + "a" * 32
PROFILE_B = "prof_" + "b" * 32
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.wall = NOW

    def monotonic(self):
        return self.value

    def utc_now(self):
        return self.wall

    def advance(self, seconds):
        self.value += seconds
        self.wall = datetime.fromtimestamp(self.wall.timestamp() + seconds, timezone.utc)


class FakeSyncer:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def sync(self, profile_id, admin_key):
        self.calls.append((profile_id, admin_key))
        return self.results.get(profile_id, ProfileSyncResult(RunStatus.SUCCESS))


class BlockingSyncer(FakeSyncer):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def sync(self, profile_id, admin_key):
        self.calls.append((profile_id, admin_key))
        self.entered.set()
        self.release.wait(timeout=2)
        return ProfileSyncResult(RunStatus.SUCCESS)


class FakeTrayBackend:
    available = True

    def __init__(self, events):
        self.events = events
        self.entries = ()

    def start(self, entries, state):
        self.entries = tuple(entries)
        self.events.append("tray.start")
        return True

    def set_state(self, state):
        self.events.append(f"tray.state:{state.value}")

    def refresh_menu(self):
        self.events.append("tray.refresh")

    def shutdown(self):
        self.events.append("tray.shutdown")


class FailingShutdownTrayBackend(FakeTrayBackend):
    def shutdown(self):
        super().shutdown()
        raise RuntimeError("simulated tray shutdown failure")


class FakeServer:
    def __init__(self, events):
        self.events = events
        self.opened = []

    def start(self):
        self.events.append("server.start")

    def open_dashboard(self, internal_path="/dashboard"):
        self.opened.append(internal_path)
        self.events.append("server.open")

    def shutdown(self):
        self.events.append("server.shutdown")


class FailingShutdownServer(FakeServer):
    def shutdown(self):
        super().shutdown()
        raise RuntimeError("simulated server shutdown failure")


class SharedActivation:
    def __init__(self):
        self.pending = False

    def request(self):
        self.pending = True
        return True

    def consume(self):
        pending, self.pending = self.pending, False
        return pending

    def clear(self):
        self.pending = False


class Reporter:
    def __init__(self):
        self.events = []

    def record(self, event, *, profile_id=None):
        self.events.append((event, profile_id))


class DeniedNotificationBackend:
    available = True

    def __init__(self):
        self.permission_requests = 0
        self.sends = 0

    def request_permission(self):
        self.permission_requests += 1
        return NotificationPermission.DENIED

    def send(self, title, body, *, on_clicked=None):
        self.sends += 1
        return True

    def shutdown(self):
        self.available = False


class FailingShutdownNotificationBackend:
    available = True

    def request_permission(self):
        return NotificationPermission.GRANTED

    def send(self, title, body, *, on_clicked=None):
        return True

    def shutdown(self):
        raise RuntimeError("simulated notification shutdown failure")


def make_profiles(*profile_ids):
    credentials = InMemoryCredentialStore()
    service = ProfileService(InMemoryProfileRepository(), clock=lambda: NOW)
    for index, profile_id in enumerate(profile_ids):
        reference = credentials.put(profile_id, f"secret-{index}")
        service.create(f"Profile {index}", reference)
    return service, credentials


def test_profile_schedulers_are_isolated_and_sleep_resume_runs_once_per_profile():
    profiles, credentials = make_profiles(PROFILE_A, PROFILE_B)
    clock = FakeClock()
    syncer = FakeSyncer(
        {PROFILE_A: ProfileSyncResult(RunStatus.AUTH_ERROR, "authentication_failed")}
    )
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        syncer,
        interval_seconds=300,
        clock=clock,
    )
    coordinator.start(run_immediately=True)

    assert coordinator.tick() is True
    assert coordinator.tick() is True
    snapshots = coordinator.snapshots()
    assert snapshots[PROFILE_A].state is SchedulerState.ERROR
    assert snapshots[PROFILE_A].next_run_monotonic is None
    assert snapshots[PROFILE_B].state is SchedulerState.MONITORING

    clock.advance(3600)
    assert coordinator.tick() is True
    assert coordinator.tick() is False
    assert [profile_id for profile_id, _secret in syncer.calls].count(PROFILE_B) == 2
    assert [profile_id for profile_id, _secret in syncer.calls].count(PROFILE_A) == 1

    coordinator.pause()
    coordinator.resume(run_immediately=True)
    assert coordinator.tick() is True
    assert [profile_id for profile_id, _secret in syncer.calls].count(PROFILE_A) == 1


def test_manual_and_background_runs_share_a_global_nonoverlap_lock():
    profiles, credentials = make_profiles(PROFILE_A, PROFILE_B)
    syncer = BlockingSyncer()
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        syncer,
        interval_seconds=300,
        clock=FakeClock(),
    )
    coordinator.start(run_immediately=True)

    worker = threading.Thread(target=lambda: coordinator.run_now(PROFILE_A))
    worker.start()
    assert syncer.entered.wait(timeout=1)
    assert coordinator.run_now(PROFILE_B) is False
    syncer.release.set()
    worker.join(timeout=2)
    assert len(syncer.calls) == 1


def test_missing_credential_stops_only_its_profile():
    profiles, credentials = make_profiles(PROFILE_A, PROFILE_B)
    credentials.delete(profiles.get(ProfileId(PROFILE_A)).credential_ref)
    reporter = Reporter()
    syncer = FakeSyncer()
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        syncer,
        interval_seconds=300,
        clock=FakeClock(),
        reporter=reporter,
    )
    coordinator.start(run_immediately=True)

    assert coordinator.tick() is True
    assert coordinator.tick() is True
    assert coordinator.snapshots()[PROFILE_A].next_run_monotonic is None
    assert coordinator.snapshots()[PROFILE_B].state is SchedulerState.MONITORING
    assert syncer.calls == [(PROFILE_B, "secret-1")]
    assert ("credential_missing", PROFILE_A) in reporter.events


def test_persistent_alert_dispatcher_uses_freshness_dedup_and_safe_deep_link(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    credentials = InMemoryCredentialStore()
    profiles = ProfileService(SQLiteProfileRepository(database), clock=lambda: NOW)
    profile = profiles.create("Profile", credentials.put(PROFILE_A, "secret"))
    state = SQLiteAlertState(database)
    state.save_rule(AlertRule("rule-50", PROFILE_A, "mini", 50))
    notifications = InMemoryNotificationAdapter()
    clock = FakeClock()
    dispatcher = PersistentAlertDispatcher(
        state,
        notifications,
        LocaleCatalog.from_directory("en"),
        freshness_seconds=900,
        clock=clock,
    )

    stale = UsageObservation(PROFILE_A, "mini", "all", date(2026, 8, 9), 40, NOW, False)
    crossed = UsageObservation(
        PROFILE_A,
        "mini",
        "project-0123456789abcdef01234567",
        date(2026, 8, 9),
        60,
        NOW,
    )
    assert dispatcher.process(profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(stale,))) == 0
    assert dispatcher.process(profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(crossed,))) == 1
    assert dispatcher.process(profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(crossed,))) == 0

    message = notifications.messages[0]
    assert PROFILE_A not in repr(message.parameters)
    assert message.parameters["profile"] == "Profile …aaaa"
    assert "project_key=project-0123456789abcdef01234567" in message.deep_link
    assert "utc_day=2026-08-09" in message.deep_link
    assert len(state.notification_history(PROFILE_A)) == 1

    clock.advance(86_400)
    next_day_stale = UsageObservation(
        PROFILE_A,
        "mini",
        "project-0123456789abcdef01234567",
        date(2026, 8, 10),
        90,
        datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
        False,
    )
    next_day_fresh = UsageObservation(
        PROFILE_A,
        "mini",
        "project-0123456789abcdef01234567",
        date(2026, 8, 10),
        90,
        datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )
    assert dispatcher.process(
        profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(next_day_stale,))
    ) == 0
    assert dispatcher.process(
        profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(next_day_fresh,))
    ) == 0


def test_notification_permission_denial_is_recorded_without_reprompting(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    credentials = InMemoryCredentialStore()
    profiles = ProfileService(SQLiteProfileRepository(database), clock=lambda: NOW)
    profile = profiles.create("Profile", credentials.put(PROFILE_A, "secret"))
    state = SQLiteAlertState(database)
    state.save_rule(AlertRule("rule-50", PROFILE_A, "mini", 50))
    state.save_rule(AlertRule("rule-80", PROFILE_A, "mini", 80))
    backend = DeniedNotificationBackend()
    notifications = LocalizedNotificationAdapter(
        backend,
        LocaleCatalog.from_directory("en"),
        open_deep_link=lambda _path: None,
    )
    dispatcher = PersistentAlertDispatcher(
        state,
        notifications,
        LocaleCatalog.from_directory("en"),
        freshness_seconds=900,
        clock=FakeClock(),
    )
    observation = UsageObservation(
        PROFILE_A, "mini", "all", date(2026, 8, 9), 90, NOW
    )

    assert dispatcher.process(
        profile, ProfileSyncResult(RunStatus.SUCCESS, observations=(observation,))
    ) == 0
    assert backend.permission_requests == 1
    assert backend.sends == 0
    history = state.notification_history(PROFILE_A)
    assert len(history) == 2
    assert {item["delivery_status"] for item in history} == {"suppressed"}
    assert {item["error_code"] for item in history} == {
        "notification_permission_denied"
    }


def test_runtime_second_instance_activates_primary_and_shutdown_order_is_clean(tmp_path):
    profiles, credentials = make_profiles(PROFILE_A)
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        FakeSyncer(),
        interval_seconds=300,
        clock=FakeClock(),
    )
    events = []
    tray = DesktopTrayAdapter(FakeTrayBackend(events), LocaleCatalog.from_directory("en"))
    registry = MemoryLockRegistry()
    activation = SharedActivation()

    def services(lock):
        return create_platform_services(
            platform_name="win32",
            environ={"APPDATA": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
            home=tmp_path,
            credential_store=credentials,
            tray=tray,
            notifications=InMemoryNotificationAdapter(),
            startup=InMemoryStartupAdapter(),
            instance_lock=lock,
            updater=InMemoryUpdaterAdapter(),
        )

    server = FakeServer(events)
    primary = DesktopRuntime(
        services(InMemoryInstanceLock("desktop", registry)),
        server,
        coordinator,
        activation,
        monitoring_enabled=True,
        open_dashboard_on_start=False,
    )
    secondary = DesktopRuntime(
        services(InMemoryInstanceLock("desktop", registry)),
        FakeServer([]),
        coordinator,
        activation,
    )

    assert primary.start(background_loop=False) is DesktopStartMode.TRAY
    assert secondary.start(background_loop=False) is DesktopStartMode.SECONDARY_ACTIVATED
    primary.poll_once()
    assert server.opened == ["/dashboard"]

    primary.shutdown()
    assert events.index("tray.shutdown") < events.index("server.shutdown")
    assert primary.running is False


def test_shutdown_releases_lock_when_native_adapters_fail(tmp_path):
    profiles, credentials = make_profiles(PROFILE_A)
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        FakeSyncer(),
        interval_seconds=300,
        clock=FakeClock(),
    )
    events = []
    reporter = Reporter()
    registry = MemoryLockRegistry()
    lock = InMemoryInstanceLock("desktop-failure", registry)
    tray = DesktopTrayAdapter(
        FailingShutdownTrayBackend(events),
        LocaleCatalog.from_directory("en"),
    )
    notifications = LocalizedNotificationAdapter(
        FailingShutdownNotificationBackend(),
        LocaleCatalog.from_directory("en"),
        open_deep_link=lambda _path: None,
    )
    services = create_platform_services(
        platform_name="win32",
        environ={"APPDATA": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
        home=tmp_path,
        credential_store=credentials,
        tray=tray,
        notifications=notifications,
        startup=InMemoryStartupAdapter(),
        instance_lock=lock,
        updater=InMemoryUpdaterAdapter(),
    )
    runtime = DesktopRuntime(
        services,
        FailingShutdownServer(events),
        coordinator,
        SharedActivation(),
        reporter=reporter,
        open_dashboard_on_start=False,
    )

    assert runtime.start(background_loop=False) is DesktopStartMode.TRAY
    runtime.shutdown()

    assert runtime.running is False
    assert "server.shutdown" in events
    assert InMemoryInstanceLock("desktop-failure", registry).acquire() is True
    assert ("notification_shutdown_failed", None) in reporter.events
    assert ("tray_shutdown_failed", None) in reporter.events
    assert ("server_shutdown_failed", None) in reporter.events


def test_headless_foreground_fallback_keeps_background_monitoring_paused(tmp_path):
    profiles, credentials = make_profiles(PROFILE_A)
    syncer = FakeSyncer()
    coordinator = ProfileSchedulerCoordinator(
        profiles,
        credentials,
        syncer,
        interval_seconds=300,
        clock=FakeClock(),
    )
    events = []
    tray = DesktopTrayAdapter(
        UnavailableTrayBackend(),
        LocaleCatalog.from_directory("en"),
    )
    services = create_platform_services(
        platform_name="linux",
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=tmp_path,
        credential_store=credentials,
        tray=tray,
        notifications=InMemoryNotificationAdapter(),
        startup=InMemoryStartupAdapter(),
        instance_lock=InMemoryInstanceLock("foreground"),
        updater=InMemoryUpdaterAdapter(),
    )
    server = FakeServer(events)
    runtime = DesktopRuntime(
        services,
        server,
        coordinator,
        SharedActivation(),
        monitoring_enabled=True,
    )

    assert runtime.start(background_loop=False) is DesktopStartMode.FOREGROUND
    runtime.toggle_pause()
    assert coordinator.paused is True
    assert syncer.calls == []
    assert server.opened == ["/dashboard"]
    runtime.shutdown()
