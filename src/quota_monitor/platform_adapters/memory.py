"""Deterministic in-memory adapters used by unit tests and local simulations."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from .contracts import (
    CredentialNotFoundError,
    CredentialReference,
    NotificationMessage,
    TrayState,
    UpdateCheckResult,
    UpdateInfo,
)


class InMemoryCredentialStore:
    """A test backend; secrets live in memory only and never appear in repr."""

    available = True

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._references: dict[str, CredentialReference] = {}
        self._accounts: dict[str, str] = {}
        self._lock = Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(items={len(self._secrets)})"

    def put(self, account_id: str, secret: str) -> CredentialReference:
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be non-empty")
        if account_id.startswith(("sk-admin-", "sk-proj-")):
            raise ValueError("account_id must not contain an API key")
        if not isinstance(secret, str) or not secret or "\x00" in secret:
            raise ValueError("secret must be a non-empty string")
        with self._lock:
            credential_id = self._accounts.get(account_id)
            if credential_id is None:
                credential_id = f"cred_{uuid4().hex}"
            reference = CredentialReference(credential_id, account_id)
            self._accounts[account_id] = credential_id
            self._references[credential_id] = reference
            self._secrets[credential_id] = secret
            return reference

    def get(self, reference: CredentialReference) -> str:
        with self._lock:
            if self._references.get(reference.credential_id) != reference:
                raise CredentialNotFoundError(reference.credential_id)
            try:
                return self._secrets[reference.credential_id]
            except KeyError:
                raise CredentialNotFoundError(reference.credential_id) from None

    def delete(self, reference: CredentialReference) -> bool:
        with self._lock:
            if self._references.get(reference.credential_id) != reference:
                return False
            del self._references[reference.credential_id]
            del self._secrets[reference.credential_id]
            self._accounts.pop(reference.account_id, None)
            return True


@dataclass
class InMemoryTrayAdapter:
    available: bool = True
    running: bool = False
    state: TrayState = TrayState.IDLE

    def start(self) -> bool:
        self.running = True
        return True

    def set_state(self, state: TrayState) -> None:
        if not self.running:
            raise RuntimeError("tray has not been started")
        self.state = state

    def shutdown(self) -> None:
        self.running = False


@dataclass
class InMemoryNotificationAdapter:
    available: bool = True
    messages: list[NotificationMessage] = field(default_factory=list)

    def send(self, message: NotificationMessage) -> bool:
        self.messages.append(message)
        return True


@dataclass
class InMemoryStartupAdapter:
    available: bool = True
    _enabled: bool = False

    def enable(self) -> bool:
        self._enabled = True
        return True

    def disable(self) -> bool:
        self._enabled = False
        return True

    def is_enabled(self) -> bool:
        return self._enabled


class MemoryLockRegistry:
    """Shared lock namespace for deterministic multi-instance tests."""

    def __init__(self) -> None:
        self._owners: set[str] = set()
        self._lock = Lock()

    def acquire(self, name: str) -> bool:
        with self._lock:
            if name in self._owners:
                return False
            self._owners.add(name)
            return True

    def release(self, name: str) -> None:
        with self._lock:
            self._owners.discard(name)


class InMemoryInstanceLock:
    available = True

    def __init__(self, name: str, registry: MemoryLockRegistry | None = None) -> None:
        if not name:
            raise ValueError("lock name must be non-empty")
        self._name = name
        self._registry = registry or MemoryLockRegistry()
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self._acquired = self._registry.acquire(self._name)
        return self._acquired

    def release(self) -> None:
        if self._acquired:
            self._registry.release(self._name)
            self._acquired = False

    def __enter__(self) -> "InMemoryInstanceLock":
        if not self.acquire():
            raise RuntimeError("another instance owns the lock")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass
class InMemoryUpdaterAdapter:
    update: UpdateInfo | None = None
    available: bool = True
    checked_versions: list[str] = field(default_factory=list)

    def check(self, current_version: str) -> UpdateCheckResult:
        self.checked_versions.append(current_version)
        if self.update is None or self.update.version == current_version:
            return UpdateCheckResult(available=False, reason="up_to_date")
        return UpdateCheckResult(available=True, update=self.update)
