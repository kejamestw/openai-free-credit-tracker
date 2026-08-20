"""Centralized, platform-aware locations for all mutable application files."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APPLICATION_DIRECTORY_NAME = "OpenAI-Free-Credit-Tracker"
ENV_PREFIX = "OPENAI_CREDIT_TRACKER_"


@dataclass(frozen=True)
class AppPaths:
    """Resolved per-user paths used by the application.

    Portable and installed builds intentionally use the same per-user locations.
    In particular, none of these paths are derived from the executable directory.
    """

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def config_backup_file(self) -> Path:
        return self.config_dir / "config.backup.json"

    @property
    def update_cache_dir(self) -> Path:
        return self.cache_dir / "updates"

    def ensure_directories(self) -> None:
        """Create the four managed directories without touching application files."""

        for path in {self.config_dir, self.data_dir, self.cache_dir, self.log_dir}:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


def resolve_app_paths(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    application_name: str = APPLICATION_DIRECTORY_NAME,
) -> AppPaths:
    """Resolve config, data, cache, and log paths for a user.

    Explicit ``OPENAI_CREDIT_TRACKER_{CONFIG,DATA,CACHE,LOG}_DIR`` overrides are
    primarily intended for managed deployments and tests. Relative overrides are
    rejected so a changed working directory cannot redirect persistent data into
    an installation or repository directory.
    """

    application_name = _application_name(application_name)
    environment = os.environ if environ is None else environ
    current_platform = (platform or sys.platform).lower()
    home_path = Path.home() if home is None else Path(home)
    if not home_path.is_absolute():
        raise ValueError("home directory must be absolute")

    if current_platform.startswith("win"):
        roaming = _environment_base(
            environment, "APPDATA", home_path / "AppData" / "Roaming"
        )
        local = _environment_base(
            environment, "LOCALAPPDATA", home_path / "AppData" / "Local"
        )
        defaults = {
            "config": roaming / application_name,
            "data": local / application_name / "Data",
            "cache": local / application_name / "Cache",
            "log": local / application_name / "Logs",
        }
    elif current_platform == "darwin":
        support = home_path / "Library" / "Application Support" / application_name
        defaults = {
            "config": support / "Config",
            "data": support / "Data",
            "cache": home_path / "Library" / "Caches" / application_name,
            "log": home_path / "Library" / "Logs" / application_name,
        }
    else:
        config_home = _environment_base(
            environment, "XDG_CONFIG_HOME", home_path / ".config"
        )
        data_home = _environment_base(
            environment, "XDG_DATA_HOME", home_path / ".local" / "share"
        )
        cache_home = _environment_base(
            environment, "XDG_CACHE_HOME", home_path / ".cache"
        )
        state_home = _environment_base(
            environment, "XDG_STATE_HOME", home_path / ".local" / "state"
        )
        defaults = {
            "config": config_home / application_name,
            "data": data_home / application_name,
            "cache": cache_home / application_name,
            "log": state_home / application_name / "log",
        }

    resolved = {
        kind: _absolute_override(environment, kind) or default
        for kind, default in defaults.items()
    }
    return AppPaths(
        config_dir=resolved["config"],
        data_dir=resolved["data"],
        cache_dir=resolved["cache"],
        log_dir=resolved["log"],
    )


def _application_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("application_name must be non-empty")
    cleaned = value.strip()
    if cleaned in {".", ".."} or any(separator in cleaned for separator in ("/", "\\")):
        raise ValueError("application_name must be a single path component")
    return cleaned


def _absolute_override(environment: Mapping[str, str], kind: str) -> Path | None:
    raw_value = environment.get(f"{ENV_PREFIX}{kind.upper()}_DIR")
    if raw_value is None:
        return None
    if not raw_value.strip():
        raise ValueError(f"{kind} directory override must not be empty")
    path = Path(raw_value)
    if not path.is_absolute():
        raise ValueError(f"{kind} directory override must be absolute")
    return path


def _environment_base(
    environment: Mapping[str, str], variable: str, default: Path
) -> Path:
    raw_value = environment.get(variable)
    if raw_value is None or not raw_value.strip():
        return default
    path = Path(raw_value)
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path
