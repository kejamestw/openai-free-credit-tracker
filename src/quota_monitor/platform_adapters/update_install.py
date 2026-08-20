"""Platform-neutral, injectable installation boundary for verified updates."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class UpdateInstallError(RuntimeError):
    """Raised when an update cannot be installed or restored safely."""


@dataclass(frozen=True)
class UpdateInstallPlan:
    """Paths selected locally by the update engine, never by a manifest."""

    staged_path: Path
    target_path: Path
    backup_path: Path
    journal_path: Path
    expected_size: int
    expected_sha256: str


@runtime_checkable
class PlatformUpdateInstaller(Protocol):
    """Small boundary implemented by in-process or privileged helper adapters."""

    def install(self, plan: UpdateInstallPlan) -> None: ...

    def rollback(self, plan: UpdateInstallPlan) -> None: ...


def _durable_copy(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    mode: int | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".new-" + uuid.uuid4().hex)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if expected_size is not None and size != expected_size:
            raise UpdateInstallError("staged update size changed before installation")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise UpdateInstallError("staged update hash changed before installation")
        os.chmod(temporary, source.stat().st_mode & 0o777 if mode is None else mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class AtomicFileUpdateInstaller:
    """Portable atomic-file adapter suitable when the target is not running."""

    def install(self, plan: UpdateInstallPlan) -> None:
        if not plan.staged_path.is_file():
            raise UpdateInstallError("staged update is unavailable")
        if not plan.target_path.is_file():
            raise UpdateInstallError("installed target is unavailable")
        target_mode = plan.target_path.stat().st_mode & 0o777
        _durable_copy(plan.target_path, plan.backup_path)
        _durable_copy(
            plan.staged_path,
            plan.target_path,
            expected_size=plan.expected_size,
            expected_sha256=plan.expected_sha256,
            mode=target_mode,
        )

    def rollback(self, plan: UpdateInstallPlan) -> None:
        if not plan.backup_path.is_file():
            raise UpdateInstallError("rollback copy is unavailable")
        _durable_copy(plan.backup_path, plan.target_path)


class FailClosedUpdateInstaller:
    """Refuse replacement when this package has no production-safe helper.

    This adapter is deliberately installed behind unsupported packaged targets
    instead of leaving an in-process file replacer reachable through recovery
    or a future call-site regression.
    """

    def install(self, plan: UpdateInstallPlan) -> None:
        del plan
        raise UpdateInstallError("automatic installation is unavailable for this package")

    def rollback(self, plan: UpdateInstallPlan) -> None:
        del plan
        raise UpdateInstallError("automatic rollback is unavailable for this package")


@dataclass(frozen=True)
class WindowsHelperPlan:
    """Serializable intent for an external helper replacing a running EXE."""

    operation: str
    staged_path: Path
    target_path: Path
    backup_path: Path
    journal_path: Path
    expected_size: int
    expected_sha256: str


class WindowsUpdateHelperRunner(Protocol):
    def execute(self, plan: WindowsHelperPlan) -> bool: ...


@dataclass(frozen=True)
class WindowsHelperUpdateInstaller:
    """Delegate replacement to an injected helper without invoking a shell."""

    runner: WindowsUpdateHelperRunner

    def install(self, plan: UpdateInstallPlan) -> None:
        if not self.runner.execute(_helper_plan("install", plan)):
            raise UpdateInstallError("Windows update helper failed")

    def rollback(self, plan: UpdateInstallPlan) -> None:
        if not self.runner.execute(_helper_plan("rollback", plan)):
            raise UpdateInstallError("Windows rollback helper failed")


def _helper_plan(operation: str, plan: UpdateInstallPlan) -> WindowsHelperPlan:
    return WindowsHelperPlan(
        operation=operation,
        staged_path=plan.staged_path,
        target_path=plan.target_path,
        backup_path=plan.backup_path,
        journal_path=plan.journal_path,
        expected_size=plan.expected_size,
        expected_sha256=plan.expected_sha256,
    )
