"""Versioned, non-sensitive application configuration and durable persistence."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .platform_paths import AppPaths
from .semver import SemVer
from .version import __version__


CONFIG_SCHEMA_VERSION = 1
MIN_REQUEST_TIMEOUT_SECONDS = 5
MAX_REQUEST_TIMEOUT_SECONDS = 300
MIN_MONITORING_INTERVAL_SECONDS = 300
MAX_RETENTION_DAYS = 3650
DEFAULT_MONITORING_INTERVAL_SECONDS = 900
DEFAULT_FRESHNESS_THRESHOLD_SECONDS = 1800
SUPPORTED_UPDATE_CHANNELS = frozenset({"stable", "beta"})


def default_update_channel() -> str:
    """Prerelease builds follow beta by default; final builds follow stable."""

    return "beta" if SemVer.parse(__version__).is_prerelease else "stable"

_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_SECRET_VALUE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\bsk-[A-Za-z0-9_-]{16,})",
    re.IGNORECASE,
)
_SENSITIVE_KEY_FRAGMENTS = (
    "adminkey",
    "apikey",
    "authorization",
    "bearertoken",
    "password",
    "privatekey",
    "rawapiresponse",
    "secret",
)
_PRIVATE_DISPLAY_NAMES = frozenset(
    {"organizationname", "organizationdisplayname", "projectname", "projectdisplayname"}
)


class ConfigError(ValueError):
    """Base class for configuration errors."""


class ConfigValidationError(ConfigError):
    """A config document does not conform to schema v1."""


class ConfigWriteError(ConfigError):
    """A validated config could not be written durably."""


class SensitiveConfigError(ConfigError):
    """An export candidate contains a prohibited key or secret-like value."""


class UnknownFieldPolicy(str, Enum):
    PRESERVE = "preserve"
    REJECT = "reject"
    IGNORE = "ignore"


class ConfigLoadSource(str, Enum):
    FILE = "file"
    BACKUP = "backup"
    DEFAULTS = "defaults"


@dataclass(frozen=True)
class UISettings:
    language: str = "zh-TW"
    open_browser_on_start: bool = True
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NetworkSettings:
    request_timeout_seconds: int = 45
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class UpdateSettings:
    channel: str = field(default_factory=default_update_channel)
    check_on_start: bool = True
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class HistorySettings:
    retention_days: int | None = None
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class MonitoringSettings:
    enabled: bool = False
    interval_seconds: int = DEFAULT_MONITORING_INTERVAL_SECONDS
    freshness_threshold_seconds: int = DEFAULT_FRESHNESS_THRESHOLD_SECONDS
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ProfilesSettings:
    active_profile_id: str | None = None
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class StartupSettings:
    enabled: bool = False
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AppConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    ui: UISettings = field(default_factory=UISettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    updates: UpdateSettings = field(default_factory=UpdateSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    profiles: ProfilesSettings = field(default_factory=ProfilesSettings)
    startup: StartupSettings = field(default_factory=StartupSettings)
    extra_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ConfigLoadResult:
    config: AppConfig
    source: ConfigLoadSource
    config_path: Path
    warning: str | None = None
    preserved_corrupt_path: Path | None = None


def default_config() -> AppConfig:
    return AppConfig()


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Convert a validated config to its stable schema-v1 representation."""

    return _config_document(validate_config(config))


def parse_config(
    document: Mapping[str, Any],
    *,
    unknown_fields: UnknownFieldPolicy = UnknownFieldPolicy.PRESERVE,
) -> AppConfig:
    """Parse and validate a config mapping without mutating the input."""

    try:
        unknown_fields = UnknownFieldPolicy(unknown_fields)
    except ValueError as error:
        raise ConfigValidationError("unknown field policy is not supported") from error
    root = _mapping(document, "config")
    root_fields = {
        "schema_version",
        "ui",
        "network",
        "updates",
        "history",
        "monitoring",
        "profiles",
        "startup",
    }
    _check_unknown(root, root_fields, "config", unknown_fields)
    _require_keys(root, {"schema_version"}, "config")

    schema_version = _integer(root["schema_version"], "schema_version")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigValidationError(
            f"schema_version must be {CONFIG_SCHEMA_VERSION}, got {schema_version}"
        )

    defaults = default_config()
    ui = _mapping(root.get("ui", {}), "ui")
    _check_unknown(ui, {"language", "open_browser_on_start"}, "ui", unknown_fields)
    language = _string(ui.get("language", defaults.ui.language), "ui.language")
    if len(language) > 35 or not _LANGUAGE_TAG.fullmatch(language):
        raise ConfigValidationError("ui.language must be a valid language tag")
    open_browser = _boolean(
        ui.get("open_browser_on_start", defaults.ui.open_browser_on_start),
        "ui.open_browser_on_start",
    )

    network = _mapping(root.get("network", {}), "network")
    _check_unknown(network, {"request_timeout_seconds"}, "network", unknown_fields)
    timeout = _integer(
        network.get(
            "request_timeout_seconds", defaults.network.request_timeout_seconds
        ),
        "network.request_timeout_seconds",
    )
    if not MIN_REQUEST_TIMEOUT_SECONDS <= timeout <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise ConfigValidationError(
            "network.request_timeout_seconds must be between "
            f"{MIN_REQUEST_TIMEOUT_SECONDS} and {MAX_REQUEST_TIMEOUT_SECONDS}"
        )

    updates = _mapping(root.get("updates", {}), "updates")
    _check_unknown(updates, {"channel", "check_on_start"}, "updates", unknown_fields)
    channel = _string(updates.get("channel", defaults.updates.channel), "updates.channel")
    if channel not in SUPPORTED_UPDATE_CHANNELS:
        allowed = ", ".join(sorted(SUPPORTED_UPDATE_CHANNELS))
        raise ConfigValidationError(f"updates.channel must be one of: {allowed}")
    check_on_start = _boolean(
        updates.get("check_on_start", defaults.updates.check_on_start),
        "updates.check_on_start",
    )

    history = _mapping(root.get("history", {}), "history")
    _check_unknown(history, {"retention_days"}, "history", unknown_fields)
    retention_days = _optional_integer(
        history.get("retention_days", defaults.history.retention_days),
        "history.retention_days",
    )
    if retention_days is not None and not 1 <= retention_days <= MAX_RETENTION_DAYS:
        raise ConfigValidationError(
            f"history.retention_days must be null or between 1 and {MAX_RETENTION_DAYS}"
        )

    monitoring = _mapping(root.get("monitoring", {}), "monitoring")
    monitoring_fields = {
        "enabled",
        "interval_seconds",
        "freshness_threshold_seconds",
    }
    _check_unknown(monitoring, monitoring_fields, "monitoring", unknown_fields)
    monitoring_enabled = _boolean(
        monitoring.get("enabled", defaults.monitoring.enabled),
        "monitoring.enabled",
    )
    interval_seconds = _integer(
        monitoring.get("interval_seconds", defaults.monitoring.interval_seconds),
        "monitoring.interval_seconds",
    )
    if interval_seconds < MIN_MONITORING_INTERVAL_SECONDS:
        raise ConfigValidationError(
            "monitoring.interval_seconds must be at least "
            f"{MIN_MONITORING_INTERVAL_SECONDS}"
        )
    freshness_threshold_seconds = _integer(
        monitoring.get(
            "freshness_threshold_seconds",
            defaults.monitoring.freshness_threshold_seconds,
        ),
        "monitoring.freshness_threshold_seconds",
    )
    if freshness_threshold_seconds < interval_seconds:
        raise ConfigValidationError(
            "monitoring.freshness_threshold_seconds must be greater than or equal "
            "to monitoring.interval_seconds"
        )

    profiles = _mapping(root.get("profiles", {}), "profiles")
    _check_unknown(profiles, {"active_profile_id"}, "profiles", unknown_fields)
    active_profile_id = _optional_string(
        profiles.get("active_profile_id", defaults.profiles.active_profile_id),
        "profiles.active_profile_id",
        maximum=200,
    )

    startup = _mapping(root.get("startup", {}), "startup")
    _check_unknown(startup, {"enabled"}, "startup", unknown_fields)
    startup_enabled = _boolean(
        startup.get("enabled", defaults.startup.enabled),
        "startup.enabled",
    )

    return AppConfig(
        schema_version=schema_version,
        ui=UISettings(
            language=language,
            open_browser_on_start=open_browser,
            extra_fields=_extras(
                ui, {"language", "open_browser_on_start"}, unknown_fields
            ),
        ),
        network=NetworkSettings(
            request_timeout_seconds=timeout,
            extra_fields=_extras(network, {"request_timeout_seconds"}, unknown_fields),
        ),
        updates=UpdateSettings(
            channel=channel,
            check_on_start=check_on_start,
            extra_fields=_extras(updates, {"channel", "check_on_start"}, unknown_fields),
        ),
        history=HistorySettings(
            retention_days=retention_days,
            extra_fields=_extras(history, {"retention_days"}, unknown_fields),
        ),
        monitoring=MonitoringSettings(
            enabled=monitoring_enabled,
            interval_seconds=interval_seconds,
            freshness_threshold_seconds=freshness_threshold_seconds,
            extra_fields=_extras(monitoring, monitoring_fields, unknown_fields),
        ),
        profiles=ProfilesSettings(
            active_profile_id=active_profile_id,
            extra_fields=_extras(profiles, {"active_profile_id"}, unknown_fields),
        ),
        startup=StartupSettings(
            enabled=startup_enabled,
            extra_fields=_extras(startup, {"enabled"}, unknown_fields),
        ),
        extra_fields=_extras(
            root, root_fields, unknown_fields
        ),
    )


def validate_config(config: AppConfig) -> AppConfig:
    if not isinstance(config, AppConfig):
        raise ConfigValidationError("config must be an AppConfig")
    # Passing through the schema parser rejects bool-as-int and validates extras.
    return parse_config(_config_document(config), unknown_fields=UnknownFieldPolicy.PRESERVE)


class ConfigService:
    """Read and atomically persist schema-v1 config without blocking startup."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        unknown_fields: UnknownFieldPolicy = UnknownFieldPolicy.PRESERVE,
    ) -> None:
        self.paths = paths
        self.unknown_fields = UnknownFieldPolicy(unknown_fields)

    def load(self) -> ConfigLoadResult:
        path = self.paths.config_file
        if not path.exists():
            return ConfigLoadResult(default_config(), ConfigLoadSource.DEFAULTS, path)

        try:
            raw = path.read_bytes()
            config = self._parse_bytes(raw)
            return ConfigLoadResult(config, ConfigLoadSource.FILE, path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ConfigError) as error:
            preserved = self._preserve_corrupt(path)
            backup_config = self._read_backup()
            warning = self._recovery_warning(error, backup_config is not None)
            if backup_config is not None:
                return ConfigLoadResult(
                    backup_config,
                    ConfigLoadSource.BACKUP,
                    path,
                    warning,
                    preserved,
                )
            return ConfigLoadResult(
                default_config(),
                ConfigLoadSource.DEFAULTS,
                path,
                warning,
                preserved,
            )

    def save(self, config: AppConfig) -> None:
        document = config_to_dict(config)
        assert_no_sensitive_data(document)
        encoded = _encode_config(document)
        path = self.paths.config_file

        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists():
                current = path.read_bytes()
                try:
                    self._parse_bytes(current)
                except (UnicodeDecodeError, json.JSONDecodeError, ConfigError):
                    self._preserve_corrupt(path)
                else:
                    _atomic_write(self.paths.config_backup_file, current)
            _atomic_write(path, encoded)
        except OSError as error:
            raise ConfigWriteError(f"could not write configuration at {path}: {error}") from error

    def reset_to_defaults(self) -> AppConfig:
        config = default_config()
        self.save(config)
        return config

    def export_json(
        self,
        config: AppConfig | None = None,
        *,
        candidate: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a pretty JSON export after recursively scanning for secrets.

        ``candidate`` exists for support tooling that augments an export. Normal
        callers should pass only ``config`` so the whitelist schema is used.
        """

        if candidate is not None and config is not None:
            raise ValueError("pass either config or candidate, not both")
        payload: Mapping[str, Any]
        if candidate is not None:
            payload = candidate
        else:
            payload = config_to_dict(config or self.load().config)
        assert_no_sensitive_data(payload)
        try:
            return (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
        except (TypeError, ValueError) as error:
            raise ConfigValidationError("export candidate must contain JSON values") from error

    def _parse_bytes(self, raw: bytes) -> AppConfig:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        assert_no_sensitive_data(document)
        return parse_config(document, unknown_fields=self.unknown_fields)

    def _read_backup(self) -> AppConfig | None:
        try:
            return self._parse_bytes(self.paths.config_backup_file.read_bytes())
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ConfigError,
        ):
            return None

    def _preserve_corrupt(self, path: Path) -> Path | None:
        if not path.is_file():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        try:
            with path.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            _restrict_permissions(destination)
        except OSError:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return destination

    @staticmethod
    def _recovery_warning(error: Exception, used_backup: bool) -> str:
        recovery = "the last valid backup" if used_backup else "safe defaults"
        return (
            f"Configuration could not be loaded ({type(error).__name__}); "
            f"using {recovery}. Repair or replace the config file before retrying."
        )


def assert_no_sensitive_data(value: Any, *, path: str = "config") -> None:
    """Reject secret-bearing fields and obvious API credentials recursively."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SensitiveConfigError(f"{path} contains a non-string field name")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if _SECRET_VALUE.search(key) or normalized in _PRIVATE_DISPLAY_NAMES or any(
                fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS
            ):
                raise SensitiveConfigError(f"sensitive field is not exportable at {path}")
            assert_no_sensitive_data(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_sensitive_data(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise SensitiveConfigError(f"secret-like value is not exportable: {path}")


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        _restrict_permissions(temporary_path)
        os.replace(temporary_path, path)
        _restrict_permissions(path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Windows ACLs are inherited from the per-user application directory.
        if os.name != "nt":
            raise


def _encode_config(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _config_document(config: AppConfig) -> dict[str, Any]:
    if not isinstance(config.ui, UISettings):
        raise ConfigValidationError("config.ui must be UISettings")
    if not isinstance(config.network, NetworkSettings):
        raise ConfigValidationError("config.network must be NetworkSettings")
    if not isinstance(config.updates, UpdateSettings):
        raise ConfigValidationError("config.updates must be UpdateSettings")
    if not isinstance(config.history, HistorySettings):
        raise ConfigValidationError("config.history must be HistorySettings")
    if not isinstance(config.monitoring, MonitoringSettings):
        raise ConfigValidationError("config.monitoring must be MonitoringSettings")
    if not isinstance(config.profiles, ProfilesSettings):
        raise ConfigValidationError("config.profiles must be ProfilesSettings")
    if not isinstance(config.startup, StartupSettings):
        raise ConfigValidationError("config.startup must be StartupSettings")
    try:
        root = deepcopy(dict(config.extra_fields))
        ui = deepcopy(dict(config.ui.extra_fields))
        network = deepcopy(dict(config.network.extra_fields))
        updates = deepcopy(dict(config.updates.extra_fields))
        history = deepcopy(dict(config.history.extra_fields))
        monitoring = deepcopy(dict(config.monitoring.extra_fields))
        profiles = deepcopy(dict(config.profiles.extra_fields))
        startup = deepcopy(dict(config.startup.extra_fields))
    except (TypeError, ValueError) as error:
        raise ConfigValidationError("extra fields must be mappings") from error
    ui.update(
        {
            "language": config.ui.language,
            "open_browser_on_start": config.ui.open_browser_on_start,
        }
    )
    network["request_timeout_seconds"] = config.network.request_timeout_seconds
    updates.update(
        {"channel": config.updates.channel, "check_on_start": config.updates.check_on_start}
    )
    history["retention_days"] = config.history.retention_days
    monitoring.update(
        {
            "enabled": config.monitoring.enabled,
            "interval_seconds": config.monitoring.interval_seconds,
            "freshness_threshold_seconds": config.monitoring.freshness_threshold_seconds,
        }
    )
    profiles["active_profile_id"] = config.profiles.active_profile_id
    startup["enabled"] = config.startup.enabled
    root.update(
        {
            "schema_version": config.schema_version,
            "ui": ui,
            "network": network,
            "updates": updates,
            "history": history,
            "monitoring": monitoring,
            "profiles": profiles,
            "startup": startup,
        }
    )
    try:
        json.dumps(root, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ConfigValidationError("extra fields must contain JSON values") from error
    return root


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigValidationError(f"duplicate field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ConfigValidationError(f"non-standard JSON number is not allowed: {value}")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigValidationError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ConfigValidationError(f"{field} field names must be strings")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigValidationError(f"{field} must be a non-empty string")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigValidationError(f"{field} must be a boolean")
    return value


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise ConfigValidationError(f"{field} must be an integer")
    return value


def _optional_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field)


def _optional_string(value: Any, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = _string(value, field)
    if len(text) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ConfigValidationError(
            f"{field} must be at most {maximum} characters without control characters"
        )
    return text


def _require_keys(value: Mapping[str, Any], required: set[str], field: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ConfigValidationError(f"{field} is missing fields: {', '.join(missing)}")


def _check_unknown(
    value: Mapping[str, Any],
    known: set[str],
    field: str,
    policy: UnknownFieldPolicy,
) -> None:
    unknown = sorted(value.keys() - known)
    if unknown and policy == UnknownFieldPolicy.REJECT:
        raise ConfigValidationError(f"{field} has unknown fields: {', '.join(unknown)}")


def _extras(
    value: Mapping[str, Any], known: set[str], policy: UnknownFieldPolicy
) -> dict[str, Any]:
    if policy is not UnknownFieldPolicy.PRESERVE:
        return {}
    return {key: deepcopy(item) for key, item in value.items() if key not in known}
