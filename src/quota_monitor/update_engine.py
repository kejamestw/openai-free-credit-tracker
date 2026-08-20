"""Consent-gated, crash-recoverable updater for authenticated manifests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .platform_adapters.update_install import PlatformUpdateInstaller, UpdateInstallPlan
from .semver import SemVer
from .update_manifest import (
    ArtifactDescriptor,
    ArtifactVerificationError,
    UpdateManifest,
    URLValidationError,
    validate_https_url,
    verify_artifact_file,
)


JOURNAL_SCHEMA_VERSION = 1
DEFAULT_DOWNLOAD_RESERVE_BYTES = 64 * 1024 * 1024


class UpdateEngineError(RuntimeError):
    pass


class UpdateStateError(UpdateEngineError):
    pass


class UpdatePolicyError(UpdateEngineError):
    pass


class ArtifactDownloadError(UpdateEngineError):
    pass


class InsufficientDiskSpaceError(ArtifactDownloadError):
    pass


class UpdateState(str, Enum):
    AVAILABLE = "available"
    DOWNLOAD_CONSENTED = "download-consented"
    DOWNLOADING = "downloading"
    VERIFIED = "verified"
    STAGED = "staged"
    INSTALL_CONSENTED = "install-consented"
    INSTALLING = "installing"
    HEALTH_CHECK = "health-check"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled-back"
    MANUAL_RECOVERY = "manual-recovery"


class ArtifactDownloader(Protocol):
    def download(
        self,
        artifact: ArtifactDescriptor,
        destination: Path,
        *,
        timeout_seconds: float,
    ) -> None: ...


class HealthChecker(Protocol):
    def check(self, *, expected_version: str, timeout_seconds: float) -> bool: ...


class DiskUsageResult(Protocol):
    free: int


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        validate_https_url(
            newurl,
            allowed_hosts=self.allowed_hosts,
            field="artifact_redirect_url",
        )
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class HttpsArtifactDownloader:
    """Download exactly one allowlisted HTTPS artifact with bounded disk use."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] | set[str],
        reserve_bytes: int = DEFAULT_DOWNLOAD_RESERVE_BYTES,
        disk_usage: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("at least one artifact host must be allowlisted")
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError("reserve_bytes must be a non-negative integer")
        self.allowed_hosts = frozenset(allowed_hosts)
        self.reserve_bytes = reserve_bytes
        self._disk_usage = disk_usage
        self._opener = urllib.request.build_opener(_SafeRedirectHandler(self.allowed_hosts))

    def download(
        self,
        artifact: ArtifactDescriptor,
        destination: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        validate_https_url(
            artifact.url,
            allowed_hosts=self.allowed_hosts,
            field="artifact_url",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        free = self._disk_usage(destination.parent).free
        if free < artifact.size + self.reserve_bytes:
            raise InsufficientDiskSpaceError("insufficient disk space for update")
        request = urllib.request.Request(
            artifact.url,
            headers={"Accept": "application/octet-stream", "User-Agent": "OpenAI-Free-Credit-Tracker"},
            method="GET",
        )
        digest = hashlib.sha256()
        total = 0
        created_destination = False
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                validate_https_url(
                    response.geturl(),
                    allowed_hosts=self.allowed_hosts,
                    field="artifact_redirect_url",
                )
                announced = response.headers.get("Content-Length")
                if announced is not None:
                    try:
                        announced_size = int(announced)
                    except (TypeError, ValueError):
                        raise ArtifactDownloadError("artifact Content-Length is invalid") from None
                    if announced_size != artifact.size:
                        raise ArtifactDownloadError("artifact Content-Length does not match manifest")
                raw_output = destination.open("xb")
                created_destination = True
                with raw_output as output:
                    while True:
                        chunk = response.read(min(1024 * 1024, artifact.size + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > artifact.size:
                            raise ArtifactDownloadError("artifact exceeds signed size")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if total != artifact.size:
                raise ArtifactDownloadError("artifact size does not match manifest")
            if digest.hexdigest() != artifact.sha256:
                raise ArtifactDownloadError("artifact SHA-256 does not match manifest")
        except (ArtifactDownloadError, InsufficientDiskSpaceError, URLValidationError):
            if created_destination:
                destination.unlink(missing_ok=True)
            raise
        except Exception as error:
            if created_destination:
                destination.unlink(missing_ok=True)
            raise ArtifactDownloadError(f"artifact download failed: {type(error).__name__}") from error


@dataclass(frozen=True)
class UpdateJournal:
    state: UpdateState
    version: str
    channel: str
    critical: bool
    manifest_fingerprint: str
    artifact_size: int
    artifact_sha256: str
    artifact_os: str | None
    artifact_arch: str | None
    artifact_format: str | None
    partial_name: str
    staged_name: str
    backup_name: str

    def as_dict(self) -> dict[str, object]:
        return {"schema_version": JOURNAL_SCHEMA_VERSION, **{
            "state": self.state.value,
            "version": self.version,
            "channel": self.channel,
            "critical": self.critical,
            "manifest_fingerprint": self.manifest_fingerprint,
            "artifact_size": self.artifact_size,
            "artifact_sha256": self.artifact_sha256,
            "artifact_os": self.artifact_os,
            "artifact_arch": self.artifact_arch,
            "artifact_format": self.artifact_format,
            "partial_name": self.partial_name,
            "staged_name": self.staged_name,
            "backup_name": self.backup_name,
        }}


class UpdateEngine:
    """Drive a verified update through consent, staging, health, and recovery."""

    def __init__(
        self,
        *,
        current_version: SemVer,
        channel: str,
        platform_os: str,
        platform_arch: str,
        artifact_format: str | None,
        cache_dir: Path,
        target_path: Path,
        downloader: ArtifactDownloader,
        installer: PlatformUpdateInstaller,
        health_checker: HealthChecker,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.current_version = current_version
        self.channel = channel
        self.platform_os = platform_os
        self.platform_arch = platform_arch
        self.artifact_format = artifact_format
        self.cache_dir = Path(cache_dir)
        self.target_path = Path(target_path)
        self.downloader = downloader
        self.installer = installer
        self.health_checker = health_checker
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.journal_path = self.cache_dir / "update-journal-v1.json"

    def prepare(self, manifest: UpdateManifest) -> UpdateState:
        artifact = self._select_artifact(manifest)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.journal_path.exists():
            existing = self.load_journal()
            if existing.state not in {UpdateState.COMMITTED, UpdateState.ROLLED_BACK}:
                raise UpdateStateError("an update is already in progress or requires recovery")
        token = uuid.uuid4().hex
        journal = UpdateJournal(
            state=UpdateState.AVAILABLE,
            version=str(manifest.version),
            channel=manifest.channel,
            critical=manifest.critical,
            manifest_fingerprint=_manifest_fingerprint(manifest),
            artifact_size=artifact.size,
            artifact_sha256=artifact.sha256,
            artifact_os=artifact.os,
            artifact_arch=artifact.arch,
            artifact_format=artifact.format,
            partial_name=f"update-{token}.partial",
            staged_name=f"update-{token}.staged",
            backup_name=f"{self.target_path.name}.previous-{token}",
        )
        self._write(journal)
        return journal.state

    def consent_download(self) -> UpdateState:
        journal = self._require(UpdateState.AVAILABLE)
        return self._transition(journal, UpdateState.DOWNLOAD_CONSENTED)

    def download(self, manifest: UpdateManifest, *, timeout_seconds: float = 60.0) -> UpdateState:
        journal = self._require(UpdateState.DOWNLOAD_CONSENTED)
        artifact = self._match_manifest(journal, manifest)
        self._transition(journal, UpdateState.DOWNLOADING)
        partial = self._cache_child(journal.partial_name)
        partial.unlink(missing_ok=True)
        self.downloader.download(artifact, partial, timeout_seconds=timeout_seconds)
        verify_artifact_file(partial, artifact)
        journal = self._replace_state(journal, UpdateState.VERIFIED)
        self._write(journal)
        staged = self._cache_child(journal.staged_name)
        staged.unlink(missing_ok=True)
        os.replace(partial, staged)
        return self._transition(journal, UpdateState.STAGED)

    def consent_install(self, manifest: UpdateManifest) -> UpdateState:
        journal = self._require(UpdateState.STAGED)
        artifact = self._match_manifest(journal, manifest)
        verify_artifact_file(self._cache_child(journal.staged_name), artifact)
        return self._transition(journal, UpdateState.INSTALL_CONSENTED)

    def install(self, manifest: UpdateManifest, *, health_timeout_seconds: float = 30.0) -> UpdateState:
        journal = self._require(UpdateState.INSTALL_CONSENTED)
        artifact = self._match_manifest(journal, manifest)
        verify_artifact_file(self._cache_child(journal.staged_name), artifact)
        plan = self._plan(journal)
        journal = self._replace_state(journal, UpdateState.INSTALLING)
        self._write(journal)
        try:
            self.installer.install(plan)
            journal = self._replace_state(journal, UpdateState.HEALTH_CHECK)
            self._write(journal)
            healthy = self.health_checker.check(
                expected_version=journal.version,
                timeout_seconds=health_timeout_seconds,
            )
            if not healthy:
                raise UpdateEngineError("updated application failed its health check")
        except Exception as error:
            return self._rollback(journal, plan, error)
        return self._transition(journal, UpdateState.COMMITTED)

    def resume(self, manifest: UpdateManifest) -> UpdateState:
        journal = self.load_journal()
        artifact = self._match_manifest(journal, manifest)
        if journal.state is UpdateState.DOWNLOADING:
            self._cache_child(journal.partial_name).unlink(missing_ok=True)
            return self._transition(journal, UpdateState.DOWNLOAD_CONSENTED)
        if journal.state is UpdateState.VERIFIED:
            partial = self._cache_child(journal.partial_name)
            staged = self._cache_child(journal.staged_name)
            candidate = partial if partial.is_file() else staged
            try:
                verify_artifact_file(candidate, artifact)
            except (OSError, ArtifactVerificationError):
                partial.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)
                return self._transition(journal, UpdateState.DOWNLOAD_CONSENTED)
            if candidate == partial:
                os.replace(partial, staged)
            return self._transition(journal, UpdateState.STAGED)
        if journal.state in {UpdateState.STAGED, UpdateState.INSTALL_CONSENTED}:
            try:
                verify_artifact_file(self._cache_child(journal.staged_name), artifact)
            except (OSError, ArtifactVerificationError):
                self._cache_child(journal.staged_name).unlink(missing_ok=True)
                return self._transition(journal, UpdateState.DOWNLOAD_CONSENTED)
            return journal.state
        if journal.state in {UpdateState.INSTALLING, UpdateState.HEALTH_CHECK}:
            return self._rollback(journal, self._plan(journal), UpdateEngineError("interrupted install"))
        return journal.state

    def load_journal(self) -> UpdateJournal:
        try:
            raw_bytes = self.journal_path.read_bytes()
            if len(raw_bytes) > 16 * 1024:
                raise ValueError("journal is too large")
            raw = json.loads(raw_bytes)
            if not isinstance(raw, dict) or raw.get("schema_version") != JOURNAL_SCHEMA_VERSION:
                raise ValueError("journal schema is invalid")
            journal = UpdateJournal(
                state=UpdateState(raw["state"]),
                version=str(raw["version"]),
                channel=str(raw["channel"]),
                critical=raw["critical"],
                manifest_fingerprint=str(raw["manifest_fingerprint"]),
                artifact_size=raw["artifact_size"],
                artifact_sha256=str(raw["artifact_sha256"]),
                artifact_os=raw.get("artifact_os"),
                artifact_arch=raw.get("artifact_arch"),
                artifact_format=raw.get("artifact_format"),
                partial_name=str(raw["partial_name"]),
                staged_name=str(raw["staged_name"]),
                backup_name=str(raw["backup_name"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise UpdateStateError("update journal is invalid") from error
        self._validate_journal(journal)
        return journal

    def _select_artifact(self, manifest: UpdateManifest) -> ArtifactDescriptor:
        self._validate_manifest_policy(manifest)
        matches = manifest.artifacts_for(os=self.platform_os, arch=self.platform_arch)
        if self.artifact_format is not None:
            matches = tuple(a for a in matches if a.format in {None, self.artifact_format})
        if len(matches) != 1:
            raise UpdatePolicyError("manifest does not contain one artifact for this platform")
        artifact = matches[0]
        if artifact.os is None and self.platform_os != "windows":
            raise UpdatePolicyError("legacy artifacts are supported only on Windows")
        return artifact

    def _validate_manifest_policy(self, manifest: UpdateManifest) -> None:
        if manifest.channel != self.channel:
            raise UpdatePolicyError("manifest channel does not match configured channel")
        if manifest.version <= self.current_version:
            raise UpdatePolicyError("manifest version is not newer than installed version")
        if manifest.expires_at is not None:
            now = self._now()
            if now.tzinfo is None:
                raise UpdateEngineError("update clock must be timezone-aware")
            if manifest.expires_at <= now:
                raise UpdatePolicyError("manifest has expired")

    def _match_manifest(self, journal: UpdateJournal, manifest: UpdateManifest) -> ArtifactDescriptor:
        artifact = self._select_artifact(manifest)
        if _manifest_fingerprint(manifest) != journal.manifest_fingerprint:
            raise UpdatePolicyError("manifest does not match the update journal")
        if (artifact.size, artifact.sha256, artifact.os, artifact.arch, artifact.format) != (
            journal.artifact_size, journal.artifact_sha256, journal.artifact_os,
            journal.artifact_arch, journal.artifact_format,
        ):
            raise UpdatePolicyError("artifact does not match the update journal")
        return artifact

    def _require(self, state: UpdateState) -> UpdateJournal:
        journal = self.load_journal()
        if journal.state is not state:
            raise UpdateStateError(f"expected update state {state.value}")
        return journal

    def _transition(self, journal: UpdateJournal, state: UpdateState) -> UpdateState:
        self._write(self._replace_state(journal, state))
        return state

    @staticmethod
    def _replace_state(journal: UpdateJournal, state: UpdateState) -> UpdateJournal:
        return UpdateJournal(**{**journal.__dict__, "state": state})

    def _rollback(self, journal: UpdateJournal, plan: UpdateInstallPlan, cause: Exception) -> UpdateState:
        try:
            self.installer.rollback(plan)
        except Exception:
            self._write(self._replace_state(journal, UpdateState.MANUAL_RECOVERY))
            return UpdateState.MANUAL_RECOVERY
        self._write(self._replace_state(journal, UpdateState.ROLLED_BACK))
        return UpdateState.ROLLED_BACK

    def _plan(self, journal: UpdateJournal) -> UpdateInstallPlan:
        return UpdateInstallPlan(
            staged_path=self._cache_child(journal.staged_name),
            target_path=self.target_path,
            backup_path=self._target_sibling(journal.backup_name),
            journal_path=self.journal_path,
            expected_size=journal.artifact_size,
            expected_sha256=journal.artifact_sha256,
        )

    def _cache_child(self, name: str) -> Path:
        return _safe_child(self.cache_dir, name)

    def _target_sibling(self, name: str) -> Path:
        return _safe_child(self.target_path.parent, name)

    def _write(self, journal: UpdateJournal) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_name(
            self.journal_path.name + ".tmp-" + uuid.uuid4().hex
        )
        encoded = json.dumps(journal.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            with temporary.open("wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_journal(self, journal: UpdateJournal) -> None:
        try:
            SemVer.parse(journal.version)
        except ValueError as error:
            raise UpdateStateError("update journal version is invalid") from error
        if journal.channel not in {"stable", "beta"} or type(journal.critical) is not bool:
            raise UpdateStateError("update journal policy fields are invalid")
        if type(journal.artifact_size) is not int or journal.artifact_size < 1:
            raise UpdateStateError("update journal artifact size is invalid")
        if len(journal.artifact_sha256) != 64 or any(c not in "0123456789abcdef" for c in journal.artifact_sha256):
            raise UpdateStateError("update journal artifact hash is invalid")
        if len(journal.manifest_fingerprint) != 64 or any(
            c not in "0123456789abcdef" for c in journal.manifest_fingerprint
        ):
            raise UpdateStateError("update journal fingerprint is invalid")
        self._cache_child(journal.partial_name)
        self._cache_child(journal.staged_name)
        self._target_sibling(journal.backup_name)


def _manifest_fingerprint(manifest: UpdateManifest) -> str:
    return hashlib.sha256(manifest.signing_payload + manifest.signature).hexdigest()


def _safe_child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise UpdateStateError("update journal contains an unsafe path")
    parent_resolved = parent.resolve()
    candidate = (parent / name).resolve()
    if candidate.parent != parent_resolved:
        raise UpdateStateError("update journal path escapes its directory")
    return candidate
