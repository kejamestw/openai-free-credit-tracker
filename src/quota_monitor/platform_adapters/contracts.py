"""Platform contracts shared by the desktop runtime.

The contracts deliberately avoid importing an operating-system SDK.  Platform
implementations can therefore be selected once at the composition root while
the application and domain services remain portable and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable


class AdapterUnavailableError(RuntimeError):
    """Raised when a requested capability has no safe platform backend."""


class CredentialNotFoundError(KeyError):
    """Raised when an opaque credential reference cannot be resolved."""


class PlatformFamily(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


class TrayState(str, Enum):
    IDLE = "idle"
    SYNCING = "syncing"
    PAUSED = "paused"
    STALE = "stale"
    ERROR = "error"


@dataclass(frozen=True)
class CredentialReference:
    """A non-secret handle to an item in an OS credential store."""

    credential_id: str
    account_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("credential_id", self.credential_id),
            ("account_id", self.account_id),
        ):
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError(f"{label} must be a non-empty opaque identifier")
            if value.startswith(("sk-admin-", "sk-proj-")):
                raise ValueError(f"{label} must be a reference, not an API key")
            if any(character.isspace() or ord(character) < 32 for character in value):
                raise ValueError(f"{label} contains an invalid character")


@dataclass(frozen=True)
class NotificationMessage:
    """A localizable notification payload that must never contain credentials."""

    title_key: str
    body_key: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    deep_link: str | None = None

    def __post_init__(self) -> None:
        for label, key in (("title_key", self.title_key), ("body_key", self.body_key)):
            if (
                not isinstance(key, str)
                or not key
                or any(character.isspace() for character in key)
            ):
                raise ValueError(f"{label} must be a translation key")
        serialized_parameters = repr(dict(self.parameters))
        secret_marker = "sk-" + "admin-"
        project_marker = "sk-" + "proj-"
        if secret_marker in serialized_parameters or project_marker in serialized_parameters:
            raise ValueError("notification parameters must not contain API credentials")
        if self.deep_link is not None:
            from .deep_links import DeepLinkValidationError, parse_deep_link

            try:
                parse_deep_link(self.deep_link)
            except DeepLinkValidationError:
                raise ValueError(
                    "deep_link must be a validated local application path/route"
                ) from None


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_uri: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("update version must be non-empty")
        from urllib.parse import urlsplit

        parsed = urlsplit(self.download_uri)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("update download_uri must use HTTPS")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise ValueError("update sha256 must be a 64-character hexadecimal digest")


@dataclass(frozen=True)
class UpdateCheckResult:
    available: bool
    update: UpdateInfo | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available != (self.update is not None):
            raise ValueError("available must match whether update metadata is present")


@runtime_checkable
class PlatformPaths(Protocol):
    @property
    def config_dir(self) -> Path: ...

    @property
    def data_dir(self) -> Path: ...

    @property
    def cache_dir(self) -> Path: ...

    @property
    def log_dir(self) -> Path: ...

    def ensure_directories(self) -> None: ...


@runtime_checkable
class CredentialStore(Protocol):
    @property
    def available(self) -> bool: ...

    def put(self, account_id: str, secret: str) -> CredentialReference: ...

    def get(self, reference: CredentialReference) -> str: ...

    def delete(self, reference: CredentialReference) -> bool: ...


@runtime_checkable
class TrayAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def start(self) -> bool: ...

    def set_state(self, state: TrayState) -> None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class NotificationAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def send(self, message: NotificationMessage) -> bool: ...


@runtime_checkable
class StartupAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def enable(self) -> bool: ...

    def disable(self) -> bool: ...

    def is_enabled(self) -> bool: ...


@runtime_checkable
class InstanceLock(Protocol):
    @property
    def available(self) -> bool: ...

    def acquire(self) -> bool: ...

    def release(self) -> None: ...


@runtime_checkable
class UpdaterAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def check(self, current_version: str) -> UpdateCheckResult: ...
