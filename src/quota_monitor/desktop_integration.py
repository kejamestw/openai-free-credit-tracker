"""Production composition for the local HTTP application and desktop runtime."""

from __future__ import annotations

import re
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .alerts import UsageObservation
from .classification import is_incentivized
from .desktop_runtime import (
    DesktopRuntime,
    DesktopServer,
    DesktopStateReporter,
    PersistentAlertDispatcher,
    ProfileSchedulerCoordinator,
    ProfileSyncResult,
)
from .i18n import LocaleCatalog
from .model_catalog import find_model, resource_path
from .openai_client import OpenAIClientError
from .operations_cli import RUNTIME_LOCK_FILENAME, build_data_runtime
from .platform_adapters import (
    FileActivationChannel,
    FileInstanceLock,
    PlatformServices,
    create_desktop_platform_services,
    parse_deep_link,
)
from .platform_paths import AppPaths
from .runtime import RuntimeServices
from .scheduler import BackoffPolicy, Clock, RunStatus, SystemClock
from .server import create_server
from .sync_service import UsageSyncService


_SAFE_PROJECT_KEY = re.compile(r"(?:unattributed|project-[0-9a-f]{24})\Z", re.ASCII)
_AUTH_ERROR_CODES = frozenset(
    {"authentication_failed", "permission_denied", "invalid_admin_key"}
)
_RETRYABLE_ERROR_CODES = frozenset(
    {"offline", "rate_limited", "upstream_timeout", "upstream_unavailable"}
)


@runtime_checkable
class LoopbackServerBackend(Protocol):
    server_address: tuple[str, int]

    def serve_forever(self, poll_interval: float = 0.5) -> None: ...

    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


class LoopbackDesktopServer:
    """Start the bound loopback server on a worker thread after lock acquisition."""

    def __init__(
        self,
        server_factory: Callable[[], LoopbackServerBackend],
        *,
        browser_open: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self._server_factory = server_factory
        self._browser_open = browser_open
        self._server: LoopbackServerBackend | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def base_url(self) -> str | None:
        with self._lock:
            server = self._server
            if server is None:
                return None
            host, port = server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            server = self._server_factory()
            host, port = server.server_address
            if host != "127.0.0.1" or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
                server.server_close()
                raise RuntimeError("desktop server did not bind a safe loopback endpoint")
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="quota-monitor-loopback",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._server = None
                self._thread = None
                server.server_close()
                raise

    def open_dashboard(self, internal_path: str = "/dashboard") -> None:
        path = parse_deep_link(internal_path).as_internal_path()
        base = self.base_url
        if base is None:
            raise RuntimeError("desktop server has not started")
        self._browser_open(base + path)

    def shutdown(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is None:
            return
        try:
            try:
                server.shutdown()
            finally:
                server.server_close()
        finally:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3.0)


class ProductionProfileSyncer:
    """Synchronize today's UTC usage and produce only pseudonymous observations."""

    def __init__(self, runtime: RuntimeServices, *, clock: Clock | None = None) -> None:
        if runtime.database is None:
            raise ValueError("desktop synchronization requires a database")
        if runtime.project_keys is None:
            raise ValueError("desktop synchronization requires project pseudonymization")
        if runtime.admin_client_factory is None:
            raise ValueError("desktop synchronization requires an upstream client factory")
        if runtime.alert_state is None:
            raise ValueError("desktop synchronization requires alert storage")
        self._runtime = runtime
        self._clock = clock or SystemClock()

    def sync(self, profile_id: str, admin_key: str) -> ProfileSyncResult:
        now = self._clock.utc_now().astimezone(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = int(day_start.timestamp())
        end_utc = int(now.timestamp())
        if end_utc <= start_utc:
            try:
                observations = self._build_observations(
                    profile_id,
                    start_utc=start_utc,
                    end_utc=None,
                    observed_at=now,
                )
            except Exception:
                return ProfileSyncResult(RunStatus.PARTIAL, "observation_build_failed")
            return ProfileSyncResult(RunStatus.SUCCESS, observations=observations)

        try:
            client = self._runtime.admin_client_factory(
                admin_key,
                self._runtime.config.network.request_timeout_seconds,
            )
            catalog_version = self._runtime.catalog.get("catalog_version")
            if not isinstance(catalog_version, str) or not catalog_version:
                return ProfileSyncResult(RunStatus.RETRYABLE_ERROR, "catalog_unavailable")
            result = UsageSyncService(
                self._runtime.database,
                project_keys=self._runtime.project_keys,
                catalog_version=catalog_version,
                profile_id=profile_id,
            ).sync(
                client,
                start_utc=start_utc,
                end_utc=end_utc,
                now=now,
                resume=True,
            )
        except OpenAIClientError as error:
            return ProfileSyncResult(self._error_status(error.code), error.code)
        except Exception:
            return ProfileSyncResult(RunStatus.RETRYABLE_ERROR, "sync_failed")

        status = self._sync_status(result.status, result.error_code)
        try:
            observations = self._build_observations(
                profile_id,
                start_utc=start_utc,
                end_utc=end_utc,
                observed_at=now,
            )
        except Exception:
            if status is RunStatus.SUCCESS:
                return ProfileSyncResult(RunStatus.PARTIAL, "observation_build_failed")
            return ProfileSyncResult(status, result.error_code or "observation_build_failed")
        return ProfileSyncResult(status, result.error_code, observations)

    @staticmethod
    def _error_status(error_code: str | None) -> RunStatus:
        if error_code in _AUTH_ERROR_CODES:
            return RunStatus.AUTH_ERROR
        return RunStatus.RETRYABLE_ERROR

    @classmethod
    def _sync_status(cls, status: str, error_code: str | None) -> RunStatus:
        if error_code in _AUTH_ERROR_CODES:
            return RunStatus.AUTH_ERROR
        if error_code in _RETRYABLE_ERROR_CODES:
            return RunStatus.RETRYABLE_ERROR
        if status == "completed":
            return RunStatus.SUCCESS
        if status == "partial":
            return RunStatus.PARTIAL
        return RunStatus.RETRYABLE_ERROR

    def _build_observations(
        self,
        profile_id: str,
        *,
        start_utc: int,
        end_utc: int | None,
        observed_at: datetime,
    ) -> tuple[UsageObservation, ...]:
        database = self._runtime.database
        alert_state = self._runtime.alert_state
        assert database is not None and alert_state is not None
        rows = (
            database.query_usage(
                start_utc,
                end_utc,
                profile_id=profile_id,
            )
            if end_utc is not None
            else []
        )
        groups = self._runtime.catalog.get("groups")
        if not isinstance(groups, dict) or not groups:
            raise ValueError("catalog groups are unavailable")

        quotas: dict[str, float] = {}
        for group_id, group in groups.items():
            quota = group.get("daily_quota_tier_1_2") if isinstance(group, dict) else None
            if (
                not isinstance(group_id, str)
                or isinstance(quota, bool)
                or not isinstance(quota, (int, float))
                or quota <= 0
            ):
                raise ValueError("catalog quota is invalid")
            quotas[group_id] = float(quota)

        project_keys = {
            row["project_key"]
            for row in rows
            if isinstance(row.get("project_key"), str)
            and _SAFE_PROJECT_KEY.fullmatch(row["project_key"])
        }
        for rule in alert_state.list_rules(profile_id):
            if rule.project_key != "all" and _SAFE_PROJECT_KEY.fullmatch(rule.project_key):
                project_keys.add(rule.project_key)

        totals = {(group_id, "all"): 0 for group_id in quotas}
        for project_key in project_keys:
            for group_id in quotas:
                totals[(group_id, project_key)] = 0
        for row in rows:
            project_key = row.get("project_key")
            if not isinstance(project_key, str) or not _SAFE_PROJECT_KEY.fullmatch(project_key):
                continue
            model = row.get("model")
            service_tier = row.get("service_tier")
            entry = find_model(model, self._runtime.catalog) if isinstance(model, str) else None
            if (
                not is_incentivized(service_tier if isinstance(service_tier, str) else None)
                or not entry
                or not entry.get("enabled", True)
                or not entry.get("eligible", True)
            ):
                continue
            group_id = entry.get("group")
            if group_id not in quotas:
                continue
            input_tokens = row.get("input_tokens")
            output_tokens = row.get("output_tokens")
            if (
                isinstance(input_tokens, bool)
                or isinstance(output_tokens, bool)
                or not isinstance(input_tokens, int)
                or not isinstance(output_tokens, int)
                or input_tokens < 0
                or output_tokens < 0
            ):
                raise ValueError("stored token total is invalid")
            total = input_tokens + output_tokens
            totals[(group_id, "all")] += total
            totals[(group_id, project_key)] += total

        utc_day = observed_at.astimezone(timezone.utc).date()
        observations: list[UsageObservation] = []
        for project_key in ("all", *sorted(project_keys)):
            for group_id in sorted(quotas):
                percent = totals[(group_id, project_key)] / quotas[group_id] * 100
                observations.append(
                    UsageObservation(
                        profile_id,
                        group_id,
                        project_key,
                        utc_day,
                        percent,
                        observed_at,
                        fresh=True,
                    )
                )
        return tuple(observations)


@dataclass(frozen=True)
class DesktopComposition:
    desktop: DesktopRuntime
    data_runtime: RuntimeServices
    server: LoopbackDesktopServer
    platform: PlatformServices
    scheduler: ProfileSchedulerCoordinator


def build_desktop_composition(
    paths: AppPaths,
    *,
    no_browser: bool = False,
    background: bool = False,
    executable: Path | None = None,
    platform_name: str | None = None,
    browser_open: Callable[[str], object] = webbrowser.open,
    data_runtime_factory: Callable[[AppPaths], RuntimeServices] | None = None,
    http_server_factory: Callable[[RuntimeServices], LoopbackServerBackend] | None = None,
    platform_services_factory: Callable[..., PlatformServices] | None = None,
    clock: Clock | None = None,
    backoff_factory: Callable[[], BackoffPolicy] | None = None,
    reporter: DesktopStateReporter | None = None,
) -> DesktopComposition:
    """Build the desktop process without starting a server or mutating native UI."""

    paths.ensure_directories()
    runtime_builder = data_runtime_factory or build_data_runtime
    runtime = runtime_builder(paths)
    if runtime.paths != paths:
        raise RuntimeError("data runtime paths do not match the desktop composition")
    if runtime.credential_store is None or runtime.profile_service is None:
        raise RuntimeError("data runtime does not provide profile credentials")
    if runtime.database is None or runtime.alert_state is None:
        raise RuntimeError("data runtime does not provide history and alerts")

    selected_clock = clock or SystemClock()
    locale = LocaleCatalog.from_directory(
        runtime.config.ui.language,
        directory=resource_path("locales"),
    )
    server_builder = http_server_factory or (
        lambda services: create_server(runtime_services=services)
    )
    server = LoopbackDesktopServer(
        lambda: server_builder(runtime),
        browser_open=browser_open,
    )

    program = (executable or Path(sys.executable)).resolve()
    startup_arguments: Sequence[str] = ("--background",)
    if not getattr(sys, "frozen", False):
        startup_arguments = ("-m", "quota_monitor", "--background")
    platform_builder = platform_services_factory or create_desktop_platform_services
    instance_lock = FileInstanceLock(
        (paths.data_dir / RUNTIME_LOCK_FILENAME).resolve()
    )
    platform = platform_builder(
        executable=program,
        locale=locale,
        open_deep_link=server.open_dashboard,
        platform_name=platform_name,
        paths=paths,
        credential_store=runtime.credential_store,
        instance_lock=instance_lock,
        startup_arguments=startup_arguments,
    )
    if platform.credential_store is not runtime.credential_store:
        raise RuntimeError("desktop composition created a second credential store")
    if platform.instance_lock is not instance_lock:
        raise RuntimeError("desktop composition created a second runtime lock")
    if platform.paths != paths:
        raise RuntimeError("desktop platform paths do not match the data runtime")
    runtime.startup_adapter = platform.startup
    runtime.notification_adapter = platform.notifications

    dispatcher = PersistentAlertDispatcher(
        runtime.alert_state,
        platform.notifications,
        locale,
        freshness_seconds=runtime.config.monitoring.freshness_threshold_seconds,
        clock=selected_clock,
    )
    syncer = ProductionProfileSyncer(runtime, clock=selected_clock)
    scheduler = ProfileSchedulerCoordinator(
        runtime.profile_service,
        runtime.credential_store,
        syncer,
        interval_seconds=runtime.config.monitoring.interval_seconds,
        clock=selected_clock,
        backoff_factory=backoff_factory,
        on_result=dispatcher.process,
        reporter=reporter,
    )
    activation = FileActivationChannel(
        (paths.cache_dir / "desktop-activation.json").resolve()
    )
    desktop = DesktopRuntime(
        platform,
        server,
        scheduler,
        activation,
        monitoring_enabled=runtime.config.monitoring.enabled,
        active_profile_id=lambda: runtime.active_profile_id,
        reporter=reporter,
        open_dashboard_on_start=(
            runtime.config.ui.open_browser_on_start and not no_browser and not background
        ),
    )
    return DesktopComposition(desktop, runtime, server, platform, scheduler)
