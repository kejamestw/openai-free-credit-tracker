"""Explicit fail-closed adapters for unavailable platform capabilities."""

from __future__ import annotations

from .contracts import (
    AdapterUnavailableError,
    CredentialReference,
    NotificationMessage,
    TrayState,
    UpdateCheckResult,
)


class _UnavailableAdapter:
    available = False

    def __init__(self, capability: str, reason: str | None = None) -> None:
        self.capability = capability
        self.reason = reason or f"{capability} backend is unavailable"

    def _raise(self) -> None:
        raise AdapterUnavailableError(self.reason)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(capability={self.capability!r})"


class UnavailableCredentialStore(_UnavailableAdapter):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("credential_store", reason)

    def put(self, account_id: str, secret: str) -> CredentialReference:
        self._raise()

    def get(self, reference: CredentialReference) -> str:
        self._raise()

    def delete(self, reference: CredentialReference) -> bool:
        self._raise()


class UnavailableTrayAdapter(_UnavailableAdapter):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("tray", reason)

    def start(self) -> bool:
        return False

    def set_state(self, state: TrayState) -> None:
        self._raise()

    def shutdown(self) -> None:
        return None


class UnavailableNotificationAdapter(_UnavailableAdapter):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("notifications", reason)

    def send(self, message: NotificationMessage) -> bool:
        return False


class UnavailableStartupAdapter(_UnavailableAdapter):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("startup", reason)

    def enable(self) -> bool:
        return False

    def disable(self) -> bool:
        return False

    def is_enabled(self) -> bool:
        return False


class UnavailableInstanceLock(_UnavailableAdapter):
    """Never claims a lock when exclusivity cannot be guaranteed."""

    def __init__(self, reason: str | None = None) -> None:
        super().__init__("instance_lock", reason)

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        return None


class UnavailableUpdaterAdapter(_UnavailableAdapter):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("updater", reason)

    def check(self, current_version: str) -> UpdateCheckResult:
        return UpdateCheckResult(available=False, reason=self.reason)
