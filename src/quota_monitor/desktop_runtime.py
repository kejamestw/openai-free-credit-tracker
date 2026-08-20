"""Desktop lifecycle, per-profile scheduling, and persistent alert delivery.

This module composes existing domain and platform contracts.  It deliberately
does not know how the loopback HTTP server, SQLite migrations, or upstream
client are implemented; those boundaries are injected by the application
composition root.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from .alert_storage import SQLiteAlertState
from .alerts import AlertEvent, UsageObservation
from .i18n import LocaleCatalog, format_number
from .platform_adapters import (
    AdapterUnavailableError,
    CredentialNotFoundError,
    CredentialStore,
    InstanceLock,
    NotificationAdapter,
    NotificationMessage,
    PlatformServices,
    StartupAdapter,
    TrayAdapter,
    TrayState,
    build_deep_link,
)
from .platform_adapters.desktop import (
    ActivationChannel,
    DesktopTrayAdapter,
    LocalizedNotificationAdapter,
    NotificationPermission,
    TrayActions,
)
from .profiles import Profile, ProfileId, ProfileNotFoundError, ProfileService
from .scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    BackoffPolicy,
    Clock,
    CollectionScheduler,
    RunResult,
    RunStatus,
    SchedulerSnapshot,
    SchedulerState,
    SystemClock,
)


@dataclass(frozen=True)
class ProfileSyncResult:
    status: RunStatus
    error_code: str | None = None
    observations: tuple[UsageObservation, ...] = ()

    def as_run_result(self) -> RunResult:
        return RunResult(self.status, self.error_code)


@runtime_checkable
class ProfileSyncer(Protocol):
    def sync(self, profile_id: str, admin_key: str) -> ProfileSyncResult: ...


@runtime_checkable
class DesktopServer(Protocol):
    def start(self) -> None: ...

    def open_dashboard(self, internal_path: str = "/dashboard") -> None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class DesktopStateReporter(Protocol):
    def record(self, event: str, *, profile_id: str | None = None) -> None: ...


class NullStateReporter:
    def record(self, event: str, *, profile_id: str | None = None) -> None:
        return None


class DesktopStartMode(str, Enum):
    TRAY = "tray"
    FOREGROUND = "foreground"
    SECONDARY_ACTIVATED = "secondary_activated"


class ProfileSchedulerCoordinator:
    """Coordinate one monotonic scheduler per profile with a global run limit."""

    def __init__(
        self,
        profiles: ProfileService,
        credentials: CredentialStore,
        syncer: ProfileSyncer,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        clock: Clock | None = None,
        backoff_factory: Callable[[], BackoffPolicy] | None = None,
        on_result: Callable[[Profile, ProfileSyncResult], None] | None = None,
        reporter: DesktopStateReporter | None = None,
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._syncer = syncer
        self._interval_seconds = interval_seconds
        self._clock = clock or SystemClock()
        self._backoff_factory = backoff_factory or BackoffPolicy
        self._on_result = on_result or (lambda _profile, _result: None)
        self._reporter = reporter or NullStateReporter()
        self._schedulers: dict[str, CollectionScheduler] = {}
        self._blocked_profiles: set[str] = set()
        self._execution_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._paused = True
        self._round_robin_offset = 0

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._paused

    def _profile(self, profile_id: str) -> Profile | None:
        try:
            profile = self._profiles.get(ProfileId(profile_id))
        except (ProfileNotFoundError, ValueError):
            return None
        return profile if profile.enabled else None

    def _collect(self, profile_id: str) -> RunResult:
        profile = self._profile(profile_id)
        if profile is None:
            with self._state_lock:
                self._blocked_profiles.add(profile_id)
            return RunResult(RunStatus.AUTH_ERROR, "profile_unavailable")
        try:
            secret = self._credentials.get(profile.credential_ref)
        except CredentialNotFoundError:
            with self._state_lock:
                self._blocked_profiles.add(profile_id)
            self._reporter.record("credential_missing", profile_id=profile_id)
            return RunResult(RunStatus.AUTH_ERROR, "credential_missing")
        except AdapterUnavailableError:
            with self._state_lock:
                self._blocked_profiles.add(profile_id)
            self._reporter.record("credential_store_unavailable", profile_id=profile_id)
            return RunResult(RunStatus.AUTH_ERROR, "credential_store_unavailable")
        except Exception:
            with self._state_lock:
                self._blocked_profiles.add(profile_id)
            self._reporter.record("credential_read_failed", profile_id=profile_id)
            return RunResult(RunStatus.AUTH_ERROR, "credential_read_failed")

        try:
            result = self._syncer.sync(profile_id, secret)
            if not isinstance(result, ProfileSyncResult):
                raise TypeError("syncer must return ProfileSyncResult")
        except Exception:
            result = ProfileSyncResult(RunStatus.RETRYABLE_ERROR, "collection_failed")
        finally:
            secret = ""

        if result.status is RunStatus.AUTH_ERROR:
            with self._state_lock:
                self._blocked_profiles.add(profile_id)

        try:
            self._on_result(profile, result)
        except Exception:
            self._reporter.record("alert_processing_failed", profile_id=profile_id)
        return result.as_run_result()

    def refresh_profiles(self) -> None:
        enabled = {profile.profile_id.value: profile for profile in self._profiles.list_enabled()}
        with self._state_lock:
            for profile_id in tuple(self._schedulers):
                if profile_id not in enabled:
                    self._schedulers[profile_id].pause()
                    del self._schedulers[profile_id]
                    self._blocked_profiles.discard(profile_id)
            for profile_id in sorted(enabled):
                if profile_id not in self._schedulers:
                    self._schedulers[profile_id] = CollectionScheduler(
                        lambda selected=profile_id: self._collect(selected),
                        interval_seconds=self._interval_seconds,
                        clock=self._clock,
                        backoff=self._backoff_factory(),
                    )

    def start(self, *, run_immediately: bool = False) -> None:
        self.refresh_profiles()
        with self._state_lock:
            self._paused = False
            schedulers = tuple(
                scheduler
                for profile_id, scheduler in self._schedulers.items()
                if profile_id not in self._blocked_profiles
            )
        for scheduler in schedulers:
            scheduler.start(run_immediately=run_immediately)

    def pause(self) -> None:
        with self._execution_lock:
            with self._state_lock:
                self._paused = True
                schedulers = tuple(self._schedulers.values())
            for scheduler in schedulers:
                scheduler.pause()

    def resume(self, *, run_immediately: bool = True) -> None:
        self.refresh_profiles()
        with self._state_lock:
            self._paused = False
            schedulers = tuple(
                scheduler
                for profile_id, scheduler in self._schedulers.items()
                if profile_id not in self._blocked_profiles
            )
        for scheduler in schedulers:
            snapshot = scheduler.snapshot()
            if snapshot.next_run_monotonic is None:
                scheduler.resume(run_immediately=run_immediately)

    def resume_profile(self, profile_id: str, *, run_immediately: bool = True) -> bool:
        self.refresh_profiles()
        with self._state_lock:
            scheduler = self._schedulers.get(profile_id)
            paused = self._paused
        if scheduler is None or paused:
            return False
        with self._state_lock:
            self._blocked_profiles.discard(profile_id)
        scheduler.resume(run_immediately=run_immediately)
        return True

    def tick(self) -> bool:
        if self.paused or not self._execution_lock.acquire(blocking=False):
            return False
        try:
            self.refresh_profiles()
            with self._state_lock:
                profile_ids = sorted(self._schedulers)
                if not profile_ids:
                    return False
                offset = self._round_robin_offset % len(profile_ids)
                ordered = profile_ids[offset:] + profile_ids[:offset]
            for profile_id in ordered:
                if self._schedulers[profile_id].tick():
                    with self._state_lock:
                        self._round_robin_offset = (profile_ids.index(profile_id) + 1) % len(
                            profile_ids
                        )
                    return True
            return False
        finally:
            self._execution_lock.release()

    def run_now(self, profile_id: str | None = None) -> bool:
        if self.paused or not self._execution_lock.acquire(blocking=False):
            return False
        try:
            self.refresh_profiles()
            with self._state_lock:
                selected = profile_id
                if selected is None:
                    selected = next(iter(sorted(self._schedulers)), None)
                scheduler = self._schedulers.get(selected or "")
            return scheduler.run_now() if scheduler is not None else False
        finally:
            self._execution_lock.release()

    def snapshots(self) -> dict[str, SchedulerSnapshot]:
        with self._state_lock:
            return {
                profile_id: scheduler.snapshot()
                for profile_id, scheduler in self._schedulers.items()
            }

    def aggregate_tray_state(self) -> TrayState:
        snapshots = tuple(self.snapshots().values())
        if self.paused or (snapshots and all(item.state is SchedulerState.PAUSED for item in snapshots)):
            return TrayState.PAUSED
        if any(item.state is SchedulerState.SYNCING for item in snapshots):
            return TrayState.SYNCING
        if any(item.state is SchedulerState.ERROR for item in snapshots):
            return TrayState.ERROR
        if any(item.state is SchedulerState.STALE for item in snapshots):
            return TrayState.STALE
        return TrayState.IDLE


class PersistentAlertDispatcher:
    """Apply freshness policy, durable deduplication, and safe notification text."""

    def __init__(
        self,
        state: SQLiteAlertState,
        notifications: NotificationAdapter,
        locale: LocaleCatalog,
        *,
        freshness_seconds: int,
        clock: Clock | None = None,
    ) -> None:
        if freshness_seconds < 1:
            raise ValueError("freshness_seconds must be positive")
        self._state = state
        self._notifications = notifications
        self._locale = locale
        self._freshness_seconds = freshness_seconds
        self._clock = clock or SystemClock()

    @staticmethod
    def _safe_profile_label(profile_id: str) -> str:
        return f"Profile …{profile_id[-4:]}"

    def _fresh(self, observation: UsageObservation, status: RunStatus) -> bool:
        if status is not RunStatus.SUCCESS or not observation.fresh:
            return False
        observed_utc = observation.observed_at.astimezone(timezone.utc)
        if observed_utc.date() != observation.utc_day:
            return False
        age = (self._clock.utc_now() - observed_utc).total_seconds()
        return -60 <= age <= self._freshness_seconds

    def _message(self, event: AlertEvent) -> NotificationMessage:
        percent = format_number(event.observed_percent, self._locale.locale, decimals=1).rstrip(
            "0"
        ).rstrip(".")
        parameters = {
            "profile": self._safe_profile_label(event.profile_id),
            "percent": percent,
        }
        deep_link_parameters = {
            "profile_id": event.profile_id,
            "view": "alerts",
            "utc_day": event.occurred_at.astimezone(timezone.utc).date().isoformat(),
            "project_key": event.project_key,
        }
        return NotificationMessage(
            "notification.quota_title",
            "notification.quota_body",
            parameters,
            deep_link=build_deep_link("/dashboard", **deep_link_parameters),
        )

    def process(self, profile: Profile, result: ProfileSyncResult) -> int:
        rules = self._state.list_rules(profile.profile_id.value)
        delivered = 0
        for original in result.observations:
            if original.profile_id != profile.profile_id.value:
                continue
            observation = replace(original, fresh=self._fresh(original, result.status))
            for event in self._state.evaluate(observation, rules):
                message = self._message(event)
                sent = self._notifications.send(message)
                if sent:
                    status, error_code = "sent", None
                    delivered += 1
                elif (
                    isinstance(self._notifications, LocalizedNotificationAdapter)
                    and self._notifications.permission is NotificationPermission.DENIED
                ):
                    status, error_code = "suppressed", "notification_permission_denied"
                else:
                    status, error_code = "failed", "notification_unavailable"
                self._state.record_notification(
                    event,
                    delivery_status=status,
                    error_code=error_code,
                )
        return delivered


class DesktopRuntime:
    """Own the server, tray, scheduler thread, activation signal, and lock."""

    def __init__(
        self,
        services: PlatformServices,
        server: DesktopServer,
        scheduler: ProfileSchedulerCoordinator,
        activation: ActivationChannel,
        *,
        monitoring_enabled: bool = False,
        active_profile_id: Callable[[], str | None] | None = None,
        reporter: DesktopStateReporter | None = None,
        poll_seconds: float = 1.0,
        open_dashboard_on_start: bool = True,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._services = services
        self._server = server
        self._scheduler = scheduler
        self._activation = activation
        self._monitoring_enabled = monitoring_enabled
        self._active_profile_id = active_profile_id or (lambda: None)
        self._reporter = reporter or NullStateReporter()
        self._poll_seconds = poll_seconds
        self._open_dashboard_on_start = open_dashboard_on_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owns_lock = False
        self._server_started = False
        self._tray_started = False
        self._shutdown_lock = threading.Lock()
        self._background_capable = False

    @property
    def running(self) -> bool:
        return self._owns_lock and not self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._scheduler.paused

    def _desktop_tray(self) -> DesktopTrayAdapter | None:
        tray = self._services.tray
        return tray if isinstance(tray, DesktopTrayAdapter) else None

    def _bind_tray(self) -> None:
        tray = self._desktop_tray()
        if tray is None:
            return
        tray.bind(
            TrayActions(
                open_dashboard=self._server.open_dashboard,
                sync_now=self.sync_now,
                toggle_pause=self.toggle_pause,
                toggle_startup=self.toggle_startup,
                show_about=lambda: self._server.open_dashboard("/settings?section=general"),
                exit=self.shutdown,
                is_paused=lambda: self._scheduler.paused,
                startup_enabled=self._services.startup.is_enabled,
            )
        )

    def start(self, *, background_loop: bool = True) -> DesktopStartMode:
        self._services.paths.ensure_directories()
        if not self._services.instance_lock.acquire():
            self._activation.request()
            self._reporter.record("secondary_instance_activated")
            return DesktopStartMode.SECONDARY_ACTIVATED
        self._owns_lock = True
        self._activation.clear()
        try:
            self._server.start()
            self._server_started = True
            self._bind_tray()
            tray = self._services.tray
            self._tray_started = bool(tray.available and tray.start())
            mode = DesktopStartMode.TRAY if self._tray_started else DesktopStartMode.FOREGROUND
            background_available = self._tray_started and self._services.credential_store.available
            self._background_capable = background_available
            if self._monitoring_enabled and background_available:
                self._scheduler.start()
            else:
                self._scheduler.pause()
                if self._monitoring_enabled:
                    self._reporter.record("background_monitoring_unavailable")
            if mode is DesktopStartMode.FOREGROUND:
                self._reporter.record("foreground_fallback")
            if self._open_dashboard_on_start:
                self._server.open_dashboard()

            self._stop.clear()
            if background_loop:
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="quota-monitor-scheduler",
                    daemon=True,
                )
                self._thread.start()
            self._update_tray_state()
            return mode
        except BaseException:
            self.shutdown()
            raise

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)

    def wait(self) -> None:
        """Keep the main process alive until tray Exit or external shutdown."""

        while self.running:
            self._stop.wait(0.5)

    def poll_once(self) -> bool:
        if not self.running:
            return False
        if self._activation.consume():
            self._server.open_dashboard()
            self._reporter.record("primary_instance_activated")
        ran = self._scheduler.tick()
        self._update_tray_state()
        return ran

    def sync_now(self) -> None:
        selected = self._active_profile_id()
        if not self._scheduler.run_now(selected):
            self._reporter.record("sync_not_started", profile_id=selected)
        self._update_tray_state()

    def toggle_pause(self) -> None:
        if self._scheduler.paused:
            if not self._background_capable:
                self._reporter.record("background_monitoring_unavailable")
                return
            self._scheduler.resume(run_immediately=True)
        else:
            self._scheduler.pause()
        tray = self._desktop_tray()
        if tray is not None:
            tray.refresh_menu()
        self._update_tray_state()

    def toggle_startup(self) -> None:
        adapter: StartupAdapter = self._services.startup
        if not adapter.available:
            self._reporter.record("startup_unavailable")
            return
        try:
            changed = adapter.disable() if adapter.is_enabled() else adapter.enable()
        except Exception:
            changed = False
        if not changed:
            self._reporter.record("startup_change_failed")
        tray = self._desktop_tray()
        if tray is not None:
            tray.refresh_menu()

    def _update_tray_state(self) -> None:
        if self._tray_started:
            try:
                self._services.tray.set_state(self._scheduler.aggregate_tray_state())
            except Exception:
                self._reporter.record("tray_state_failed")

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if not self._owns_lock and not self._server_started and not self._tray_started:
                return
            self._stop.set()
            try:
                self._scheduler.pause()
            except Exception:
                self._reporter.record("scheduler_shutdown_failed")
            if isinstance(self._services.notifications, LocalizedNotificationAdapter):
                try:
                    self._services.notifications.shutdown()
                except Exception:
                    self._reporter.record("notification_shutdown_failed")
            try:
                self._services.tray.shutdown()
            except Exception:
                self._reporter.record("tray_shutdown_failed")
            self._tray_started = False
            if self._server_started:
                try:
                    self._server.shutdown()
                except Exception:
                    self._reporter.record("server_shutdown_failed")
                finally:
                    self._server_started = False
            try:
                self._activation.clear()
            except Exception:
                self._reporter.record("activation_cleanup_failed")
            self._background_capable = False
            if self._owns_lock:
                try:
                    self._services.instance_lock.release()
                except Exception:
                    self._reporter.record("instance_lock_release_failed")
                finally:
                    self._owns_lock = False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._poll_seconds * 2))
        self._thread = None
