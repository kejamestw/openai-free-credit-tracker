"""The only composition point that selects platform-specific capabilities."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import (
    CredentialStore,
    InstanceLock,
    NotificationAdapter,
    PlatformFamily,
    StartupAdapter,
    TrayAdapter,
    UpdaterAdapter,
)
from .fallback import (
    UnavailableCredentialStore,
    UnavailableInstanceLock,
    UnavailableNotificationAdapter,
    UnavailableStartupAdapter,
    UnavailableTrayAdapter,
    UnavailableUpdaterAdapter,
)
from .paths import ResolvedPlatformPaths, resolve_platform_paths


@dataclass(frozen=True)
class PlatformCapabilities:
    family: PlatformFamily
    credential_store: bool
    tray: bool
    notifications: bool
    startup: bool
    instance_lock: bool
    updater: bool


@dataclass(frozen=True)
class PlatformServices:
    family: PlatformFamily
    paths: ResolvedPlatformPaths
    credential_store: CredentialStore
    tray: TrayAdapter
    notifications: NotificationAdapter
    startup: StartupAdapter
    instance_lock: InstanceLock
    updater: UpdaterAdapter
    capabilities: PlatformCapabilities


def detect_platform_family(platform_name: str | None = None) -> PlatformFamily:
    name = (platform_name or sys.platform).lower()
    if name.startswith("win"):
        return PlatformFamily.WINDOWS
    if name == "darwin":
        return PlatformFamily.MACOS
    if name.startswith("linux"):
        return PlatformFamily.LINUX
    return PlatformFamily.UNKNOWN


def detect_capabilities(
    family: PlatformFamily,
    *,
    credential_store: CredentialStore,
    tray: TrayAdapter,
    notifications: NotificationAdapter,
    startup: StartupAdapter,
    instance_lock: InstanceLock,
    updater: UpdaterAdapter,
) -> PlatformCapabilities:
    return PlatformCapabilities(
        family=family,
        credential_store=credential_store.available,
        tray=tray.available,
        notifications=notifications.available,
        startup=startup.available,
        instance_lock=instance_lock.available,
        updater=updater.available,
    )


def create_platform_services(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    credential_store: CredentialStore | None = None,
    tray: TrayAdapter | None = None,
    notifications: NotificationAdapter | None = None,
    startup: StartupAdapter | None = None,
    instance_lock: InstanceLock | None = None,
    updater: UpdaterAdapter | None = None,
) -> PlatformServices:
    """Build a platform service bundle without performing external actions.

    Real OS backends must be injected explicitly.  Until they are available,
    callers receive honest unavailable adapters instead of plaintext or
    otherwise unsafe emulation.
    """

    family = detect_platform_family(platform_name)
    selected_credential_store = credential_store or UnavailableCredentialStore()
    selected_tray = tray or UnavailableTrayAdapter()
    selected_notifications = notifications or UnavailableNotificationAdapter()
    selected_startup = startup or UnavailableStartupAdapter()
    selected_instance_lock = instance_lock or UnavailableInstanceLock()
    selected_updater = updater or UnavailableUpdaterAdapter()
    capabilities = detect_capabilities(
        family,
        credential_store=selected_credential_store,
        tray=selected_tray,
        notifications=selected_notifications,
        startup=selected_startup,
        instance_lock=selected_instance_lock,
        updater=selected_updater,
    )
    return PlatformServices(
        family=family,
        paths=resolve_platform_paths(family, environ=environ, home=home),
        credential_store=selected_credential_store,
        tray=selected_tray,
        notifications=selected_notifications,
        startup=selected_startup,
        instance_lock=selected_instance_lock,
        updater=selected_updater,
        capabilities=capabilities,
    )
