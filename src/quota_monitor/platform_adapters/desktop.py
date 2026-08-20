"""Desktop platform adapters and the production desktop composition factory.

All imports of optional GUI libraries are isolated in this module.  Importing
the package never starts a GUI loop, requests notification permission, or
changes startup settings.  Tests inject the small backend and filesystem
boundaries below instead of mutating the host desktop.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from quota_monitor.i18n import LocaleCatalog

from .contracts import (
    CredentialStore,
    InstanceLock,
    NotificationAdapter,
    NotificationMessage,
    PlatformFamily,
    PlatformPaths,
    StartupAdapter,
    TrayAdapter,
    TrayState,
    UpdaterAdapter,
)
from .credentials import create_native_credential_store
from .deep_links import parse_deep_link
from .factory import (
    PlatformServices,
    create_platform_services,
    detect_capabilities,
    detect_platform_family,
)
from .fallback import UnavailableUpdaterAdapter
from .instance_lock import FileInstanceLock
from .startup import (
    LinuxStartupAdapter,
    LocalStartupRunner,
    MacOSStartupAdapter,
    StartupRunner,
    WindowsStartupAdapter,
)


class TrayAction(str, Enum):
    OPEN_DASHBOARD = "open_dashboard"
    SYNC_NOW = "sync_now"
    TOGGLE_PAUSE = "toggle_pause"
    TOGGLE_STARTUP = "toggle_startup"
    ABOUT = "about"
    EXIT = "exit"


@dataclass(frozen=True)
class TrayActions:
    open_dashboard: Callable[[], None]
    sync_now: Callable[[], None]
    toggle_pause: Callable[[], None]
    toggle_startup: Callable[[], None]
    show_about: Callable[[], None]
    exit: Callable[[], None]
    is_paused: Callable[[], bool] = lambda: False
    startup_enabled: Callable[[], bool] = lambda: False


@dataclass(frozen=True)
class TrayMenuEntry:
    action: TrayAction
    label: Callable[[], str]
    invoke: Callable[[], None]
    checked: Callable[[], bool] | None = None
    default: bool = False


@runtime_checkable
class TrayBackend(Protocol):
    @property
    def available(self) -> bool: ...

    def start(self, entries: Sequence[TrayMenuEntry], state: TrayState) -> bool: ...

    def set_state(self, state: TrayState) -> None: ...

    def refresh_menu(self) -> None: ...

    def shutdown(self) -> None: ...


class UnavailableTrayBackend:
    available = False

    def start(self, entries: Sequence[TrayMenuEntry], state: TrayState) -> bool:
        return False

    def set_state(self, state: TrayState) -> None:
        return None

    def refresh_menu(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class DesktopTrayAdapter:
    """Bind the six roadmap tray actions to an injected native backend."""

    def __init__(self, backend: TrayBackend, locale: LocaleCatalog) -> None:
        self._backend = backend
        self._locale = locale
        self._actions: TrayActions | None = None
        self._state = TrayState.IDLE
        self._running = False

    @property
    def available(self) -> bool:
        return self._backend.available

    @property
    def running(self) -> bool:
        return self._running

    def bind(self, actions: TrayActions) -> None:
        self._actions = actions

    def _text(self, key: str, fallback: str) -> str:
        return self._locale.translate(key, fallback_text=fallback)

    def _entries(self) -> tuple[TrayMenuEntry, ...]:
        actions = self._actions
        if actions is None:
            raise RuntimeError("tray actions have not been bound")
        return (
            TrayMenuEntry(
                TrayAction.OPEN_DASHBOARD,
                lambda: self._text("tray.open_dashboard", "Open Dashboard"),
                actions.open_dashboard,
                default=True,
            ),
            TrayMenuEntry(
                TrayAction.SYNC_NOW,
                lambda: self._text("tray.sync_now", "Sync now"),
                actions.sync_now,
            ),
            TrayMenuEntry(
                TrayAction.TOGGLE_PAUSE,
                lambda: self._text(
                    "tray.resume" if actions.is_paused() else "tray.pause",
                    "Resume monitoring" if actions.is_paused() else "Pause monitoring",
                ),
                actions.toggle_pause,
            ),
            TrayMenuEntry(
                TrayAction.TOGGLE_STARTUP,
                lambda: self._text("settings.startup", "Start automatically at sign-in"),
                actions.toggle_startup,
                checked=actions.startup_enabled,
            ),
            TrayMenuEntry(
                TrayAction.ABOUT,
                lambda: self._text("tray.about", "About"),
                actions.show_about,
            ),
            TrayMenuEntry(
                TrayAction.EXIT,
                lambda: self._text("tray.exit", "Exit"),
                actions.exit,
            ),
        )

    def start(self) -> bool:
        if not self.available:
            return False
        self._running = bool(self._backend.start(self._entries(), self._state))
        return self._running

    def set_state(self, state: TrayState) -> None:
        self._state = state
        if self._running:
            self._backend.set_state(state)

    def refresh_menu(self) -> None:
        if self._running:
            self._backend.refresh_menu()

    def shutdown(self) -> None:
        self._running = False
        self._backend.shutdown()


class PystrayBackend:
    """Thin adapter over pystray with runtime capability checks."""

    _STATE_TITLES = {
        TrayState.IDLE: "OpenAI Free Credit Tracker",
        TrayState.SYNCING: "OpenAI Free Credit Tracker — syncing",
        TrayState.PAUSED: "OpenAI Free Credit Tracker — paused",
        TrayState.STALE: "OpenAI Free Credit Tracker — stale",
        TrayState.ERROR: "OpenAI Free Credit Tracker — action required",
    }

    def __init__(self, pystray_module: object, image_module: object, draw_module: object) -> None:
        self._pystray = pystray_module
        self._image = image_module
        self._draw = draw_module
        icon_type = getattr(pystray_module, "Icon", None)
        self.available = bool(icon_type is not None and getattr(icon_type, "HAS_MENU", False))
        self._icon: object | None = None

    def _image_for(self, state: TrayState) -> object:
        colors = {
            TrayState.IDLE: "#22c55e",
            TrayState.SYNCING: "#3b82f6",
            TrayState.PAUSED: "#94a3b8",
            TrayState.STALE: "#f59e0b",
            TrayState.ERROR: "#ef4444",
        }
        image = self._image.new("RGBA", (64, 64), (0, 0, 0, 0))
        canvas = self._draw.Draw(image)
        canvas.ellipse((5, 5, 59, 59), fill=colors[state])
        canvas.ellipse((20, 20, 44, 44), fill="white")
        return image

    def start(self, entries: Sequence[TrayMenuEntry], state: TrayState) -> bool:
        if not self.available or self._icon is not None:
            return self._icon is not None

        item_type = getattr(self._pystray, "MenuItem")
        menu_type = getattr(self._pystray, "Menu")

        def menu_item(entry: TrayMenuEntry) -> object:
            def invoke(_icon: object, _item: object) -> None:
                entry.invoke()
                self.refresh_menu()

            return item_type(
                lambda _item: entry.label(),
                invoke,
                checked=(
                    (lambda _item: entry.checked()) if entry.checked is not None else None
                ),
                default=entry.default,
            )

        try:
            menu = menu_type(*(menu_item(entry) for entry in entries))
            icon = getattr(self._pystray, "Icon")(
                "openai-free-credit-tracker",
                self._image_for(state),
                self._STATE_TITLES[state],
                menu,
            )
            icon.run_detached()
            self._icon = icon
        except Exception:
            self.available = False
            self._icon = None
            return False
        return True

    def set_state(self, state: TrayState) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = self._image_for(state)
            icon.title = self._STATE_TITLES[state]
            icon.update_menu()
        except Exception:
            self.available = False

    def refresh_menu(self) -> None:
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                self.available = False

    def shutdown(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                return


def desktop_session_available(
    family: PlatformFamily,
    *,
    environ: Mapping[str, str] | None = None,
    host_platform: str | None = None,
) -> bool:
    environment = os.environ if environ is None else environ
    host = (host_platform or sys.platform).lower()
    if detect_platform_family(host) is not family:
        return False
    if family is PlatformFamily.LINUX:
        display = bool(environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY"))
        return display and bool(environment.get("DBUS_SESSION_BUS_ADDRESS"))
    return family in {PlatformFamily.WINDOWS, PlatformFamily.MACOS}


def create_native_tray_adapter(
    family: PlatformFamily,
    locale: LocaleCatalog,
    *,
    environ: Mapping[str, str] | None = None,
) -> DesktopTrayAdapter:
    if not desktop_session_available(family, environ=environ):
        return DesktopTrayAdapter(UnavailableTrayBackend(), locale)
    try:
        import pystray  # type: ignore[import-not-found]
        from PIL import Image, ImageDraw  # type: ignore[import-not-found]

        backend: TrayBackend = PystrayBackend(pystray, Image, ImageDraw)
    except (ImportError, OSError, RuntimeError):
        backend = UnavailableTrayBackend()
    return DesktopTrayAdapter(backend, locale)


class NotificationPermission(str, Enum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class NotificationPermissionDenied(RuntimeError):
    pass


@runtime_checkable
class NotificationBackend(Protocol):
    @property
    def available(self) -> bool: ...

    def request_permission(self) -> NotificationPermission: ...

    def send(
        self,
        title: str,
        body: str,
        *,
        on_clicked: Callable[[], None] | None = None,
    ) -> bool: ...

    def shutdown(self) -> None: ...


class UnavailableNotificationBackend:
    available = False

    def request_permission(self) -> NotificationPermission:
        return NotificationPermission.UNAVAILABLE

    def send(
        self,
        title: str,
        body: str,
        *,
        on_clicked: Callable[[], None] | None = None,
    ) -> bool:
        return False

    def shutdown(self) -> None:
        return None


class DesktopNotifierBackend:
    """Keep one asyncio loop alive so notification click callbacks remain usable."""

    available = True

    def __init__(
        self,
        notifier: object,
        *,
        timeout_seconds: float = 10.0,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] = asyncio.new_event_loop,
    ) -> None:
        self._notifier = notifier
        self._timeout_seconds = timeout_seconds
        self._loop_factory = loop_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def _run_loop(self) -> None:
        loop, ready = self._loop, self._ready
        if loop is None or ready is None:
            return
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    def _ensure_loop(self) -> bool:
        if self._loop is not None and self._loop.is_running():
            return True
        self._loop = self._loop_factory()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="quota-monitor-notifications",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._timeout_seconds):
            self.available = False
        return self.available

    def request_permission(self) -> NotificationPermission:
        if not self.available or not self._ensure_loop():
            return NotificationPermission.UNAVAILABLE
        try:
            coroutine = self._notifier.request_authorisation()
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            granted = bool(future.result(timeout=self._timeout_seconds))
        except Exception as error:
            if type(error).__name__ in {"AuthorisationError", "PermissionError"}:
                return NotificationPermission.DENIED
            self.available = False
            return NotificationPermission.UNAVAILABLE
        return NotificationPermission.GRANTED if granted else NotificationPermission.DENIED

    def send(
        self,
        title: str,
        body: str,
        *,
        on_clicked: Callable[[], None] | None = None,
    ) -> bool:
        if not self.available or not self._ensure_loop():
            return False
        try:
            coroutine = self._notifier.send(
                title=title,
                message=body,
                on_clicked=on_clicked,
            )
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            future.result(timeout=self._timeout_seconds)
        except Exception as error:
            if type(error).__name__ in {"AuthorisationError", "PermissionError"}:
                raise NotificationPermissionDenied("notification permission was denied") from None
            self.available = False
            return False
        return True

    def shutdown(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._timeout_seconds)
        if loop is not None and not loop.is_running() and not loop.is_closed():
            loop.close()
        self._loop = None
        self._thread = None
        self.available = False


class LocalizedNotificationAdapter:
    """Translate safe notification contracts and remember denied permission."""

    def __init__(
        self,
        backend: NotificationBackend,
        locale: LocaleCatalog,
        *,
        open_deep_link: Callable[[str], None],
    ) -> None:
        self._backend = backend
        self._locale = locale
        self._open_deep_link = open_deep_link
        self._permission = (
            NotificationPermission.UNKNOWN
            if backend.available
            else NotificationPermission.UNAVAILABLE
        )
        self._permission_attempted = False

    @property
    def available(self) -> bool:
        return self._backend.available and self._permission not in {
            NotificationPermission.DENIED,
            NotificationPermission.UNAVAILABLE,
        }

    @property
    def permission(self) -> NotificationPermission:
        return self._permission

    @property
    def permission_attempted(self) -> bool:
        return self._permission_attempted

    def mark_permission_denied(self) -> None:
        self._permission = NotificationPermission.DENIED
        self._permission_attempted = True

    def send(self, message: NotificationMessage) -> bool:
        if not self.available:
            return False
        if not self._permission_attempted:
            self._permission_attempted = True
            self._permission = self._backend.request_permission()
            if self._permission is not NotificationPermission.GRANTED:
                return False
        title = self._locale.translate(
            message.title_key,
            fallback_text="OpenAI Free Credit Tracker",
            parameters=message.parameters,
        )
        body = self._locale.translate(
            message.body_key,
            fallback_text="Quota status changed.",
            parameters=message.parameters,
        )
        on_clicked: Callable[[], None] | None = None
        if message.deep_link is not None:
            internal_path = parse_deep_link(message.deep_link).as_internal_path()
            on_clicked = lambda: self._open_deep_link(internal_path)
        try:
            delivered = self._backend.send(title, body, on_clicked=on_clicked)
        except NotificationPermissionDenied:
            self.mark_permission_denied()
            return False
        if not delivered and not self._backend.available:
            self._permission = NotificationPermission.UNAVAILABLE
        return delivered

    def shutdown(self) -> None:
        self._backend.shutdown()


def create_native_notification_adapter(
    family: PlatformFamily,
    locale: LocaleCatalog,
    *,
    open_deep_link: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
) -> LocalizedNotificationAdapter:
    if not desktop_session_available(family, environ=environ):
        backend: NotificationBackend = UnavailableNotificationBackend()
    else:
        try:
            from desktop_notifier import DesktopNotifier  # type: ignore[import-not-found]

            loop_factory: Callable[[], asyncio.AbstractEventLoop] = asyncio.new_event_loop
            if family is PlatformFamily.MACOS:
                from rubicon.objc.eventloop import EventLoopPolicy  # type: ignore[import-not-found]

                loop_factory = EventLoopPolicy().new_event_loop
            backend = DesktopNotifierBackend(
                DesktopNotifier(app_name="OpenAI Free Credit Tracker"),
                loop_factory=loop_factory,
            )
        except (ImportError, OSError, RuntimeError):
            backend = UnavailableNotificationBackend()
    return LocalizedNotificationAdapter(backend, locale, open_deep_link=open_deep_link)


@runtime_checkable
class ActivationFileRunner(Protocol):
    def atomic_write(self, path: Path, content: bytes) -> None: ...

    def read(self, path: Path) -> bytes | None: ...

    def remove(self, path: Path) -> None: ...


class LocalActivationFileRunner:
    def atomic_write(self, path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def read(self, path: Path) -> bytes | None:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size > 1024:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def remove(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


@runtime_checkable
class ActivationChannel(Protocol):
    def request(self) -> bool: ...

    def consume(self) -> bool: ...

    def clear(self) -> None: ...


class FileActivationChannel:
    """Wake an existing instance without publishing its random loopback port."""

    def __init__(self, path: Path, runner: ActivationFileRunner | None = None) -> None:
        self._path = Path(path)
        if not self._path.is_absolute():
            raise ValueError("activation path must be absolute")
        self._runner = runner or LocalActivationFileRunner()

    @property
    def path(self) -> Path:
        return self._path

    def request(self) -> bool:
        payload = json.dumps(
            {"action": "open_dashboard", "nonce": uuid.uuid4().hex},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        try:
            self._runner.atomic_write(self._path, payload)
        except OSError:
            return False
        return True

    def consume(self) -> bool:
        raw = self._runner.read(self._path)
        if raw is None:
            return False
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("action") != "open_dashboard"
            or not isinstance(payload.get("nonce"), str)
            or len(payload["nonce"]) != 32
        ):
            return False
        self._runner.remove(self._path)
        return True

    def clear(self) -> None:
        self._runner.remove(self._path)


def create_native_startup_adapter(
    family: PlatformFamily,
    *,
    executable: Path,
    paths: object,
    home: Path,
    runner: StartupRunner | None = None,
    arguments: Sequence[str] = ("--background",),
) -> StartupAdapter:
    selected_runner = runner or LocalStartupRunner()
    if family is PlatformFamily.WINDOWS:
        return WindowsStartupAdapter(executable, arguments, runner=selected_runner)
    if family is PlatformFamily.MACOS:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return MacOSStartupAdapter(
            executable,
            home / "Library" / "LaunchAgents",
            arguments,
            uid=uid,
            runner=selected_runner,
        )
    if family is PlatformFamily.LINUX:
        return LinuxStartupAdapter(
            executable,
            Path(getattr(paths, "config_dir")).parent / "autostart",
            arguments,
            runner=selected_runner,
        )
    from .fallback import UnavailableStartupAdapter

    return UnavailableStartupAdapter("startup registration is unsupported")


def create_desktop_platform_services(
    *,
    executable: Path,
    locale: LocaleCatalog,
    open_deep_link: Callable[[str], None],
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    paths: PlatformPaths | None = None,
    credential_store: CredentialStore | None = None,
    tray: TrayAdapter | None = None,
    notifications: NotificationAdapter | None = None,
    startup: StartupAdapter | None = None,
    instance_lock: InstanceLock | None = None,
    updater: UpdaterAdapter | None = None,
    startup_runner: StartupRunner | None = None,
    startup_arguments: Sequence[str] = ("--background",),
) -> PlatformServices:
    """Create inert production adapters with native, fail-closed defaults."""

    program = Path(executable)
    if not program.is_absolute():
        raise ValueError("desktop executable path must be absolute")
    family = detect_platform_family(platform_name)
    base = (
        create_platform_services(
            platform_name=platform_name,
            environ=environ,
            home=home,
        )
        if paths is None
        else None
    )
    selected_paths = base.paths if base is not None else paths
    assert selected_paths is not None
    selected_home = Path(home) if home is not None else Path.home()
    if credential_store is not None:
        selected_credential = credential_store
    elif family is PlatformFamily.LINUX and not (
        os.environ if environ is None else environ
    ).get("DBUS_SESSION_BUS_ADDRESS"):
        from .fallback import UnavailableCredentialStore

        selected_credential = UnavailableCredentialStore(
            "Linux Secret Service requires a desktop DBus session"
        )
    else:
        selected_credential = create_native_credential_store(family)
    selected_tray = tray or create_native_tray_adapter(family, locale, environ=environ)
    selected_notifications = notifications or create_native_notification_adapter(
        family,
        locale,
        open_deep_link=open_deep_link,
        environ=environ,
    )
    selected_startup = startup or create_native_startup_adapter(
        family,
        executable=program,
        paths=selected_paths,
        home=selected_home,
        runner=startup_runner,
        arguments=startup_arguments,
    )
    selected_lock = instance_lock or FileInstanceLock(
        (selected_paths.cache_dir / "desktop-runtime.lock").resolve()
    )
    selected_updater = updater or UnavailableUpdaterAdapter()
    capabilities = detect_capabilities(
        family,
        credential_store=selected_credential,
        tray=selected_tray,
        notifications=selected_notifications,
        startup=selected_startup,
        instance_lock=selected_lock,
        updater=selected_updater,
    )
    return PlatformServices(
        family=family,
        paths=selected_paths,
        credential_store=selected_credential,
        tray=selected_tray,
        notifications=selected_notifications,
        startup=selected_startup,
        instance_lock=selected_lock,
        updater=selected_updater,
        capabilities=capabilities,
    )
