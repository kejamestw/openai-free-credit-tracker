"""Compatibility adapter for the application's single platform path service."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ..platform_paths import (
    APPLICATION_DIRECTORY_NAME,
    AppPaths,
    resolve_app_paths,
)
from .contracts import PlatformFamily


DEFAULT_APP_DIRECTORY = APPLICATION_DIRECTORY_NAME
ResolvedPlatformPaths = AppPaths


def resolve_platform_paths(
    family: PlatformFamily,
    *,
    app_directory: str = DEFAULT_APP_DIRECTORY,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ResolvedPlatformPaths:
    """Resolve through :mod:`quota_monitor.platform_paths`.

    This wrapper preserves the v0.4 platform-adapter API while preventing the
    desktop composition layer and the config/database layer from disagreeing
    about where persistent files live.
    """

    platform = {
        PlatformFamily.WINDOWS: "win32",
        PlatformFamily.MACOS: "darwin",
        PlatformFamily.LINUX: "linux",
        PlatformFamily.UNKNOWN: "linux",
    }[family]
    return resolve_app_paths(
        platform=platform,
        environ=environ,
        home=home,
        application_name=app_directory,
    )
