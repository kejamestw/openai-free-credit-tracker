"""Product-facing orchestration for the authenticated update engine.

The HTTP boundary never supplies a URL or filesystem path.  A bundled trust
document selects fixed per-channel manifest sources and a bounded keyring, while the engine derives
all mutable paths from the application cache and current executable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .platform_adapters.update_install import (
    AtomicFileUpdateInstaller,
    FailClosedUpdateInstaller,
)
from .platform_paths import AppPaths
from .semver import SemVer
from .update_crypto import build_ed25519_keyring, decode_base64_key
from .update_engine import (
    ArtifactDownloadError,
    HttpsArtifactDownloader,
    InsufficientDiskSpaceError,
    UpdateEngine,
    UpdateEngineError,
    UpdatePolicyError,
    UpdateState,
    UpdateStateError,
)
from .update_manifest import (
    ManifestFetcher,
    ManifestParser,
    ManifestPolicy,
    UpdateChecker,
    UpdateCheckResult,
    UpdateManifest,
    UpdateStatus,
    UrllibManifestFetcher,
    validate_https_url,
)


TRUST_SCHEMA_VERSION = 1
RUNTIME_STATUS_SCHEMA_VERSION = 1
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_TRUST_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_NONTERMINAL_STATES = frozenset(
    state for state in UpdateState if state not in {UpdateState.COMMITTED, UpdateState.ROLLED_BACK}
)
_RECOVERY_STATES = frozenset(
    {
        UpdateState.DOWNLOADING,
        UpdateState.VERIFIED,
        UpdateState.INSTALLING,
        UpdateState.HEALTH_CHECK,
        UpdateState.MANUAL_RECOVERY,
    }
)


class UpdateRuntimeError(RuntimeError):
    """A stable, secret-free product action failure."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AuthenticatedUpdateSource(Protocol):
    def check(self) -> UpdateCheckResult: ...


@dataclass(frozen=True)
class RemoteAuthenticatedUpdateSource:
    checker: UpdateChecker
    manifest_url: str
    fetcher: ManifestFetcher
    timeout_seconds: float = 10.0

    def check(self) -> UpdateCheckResult:
        return self.checker.check_remote(
            self.manifest_url,
            fetcher=self.fetcher,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class ProductUpdateTrust:
    manifest_urls: Mapping[str, str]
    manifest_hosts: frozenset[str]
    artifact_hosts: frozenset[str]
    release_notes_hosts: frozenset[str]
    public_keys: Mapping[str, bytes]


class ExecutableSmokeHealthChecker:
    """Verify the installed version, then run its side-effect-free smoke check."""

    def __init__(self, executable: Path) -> None:
        self.executable = Path(executable).resolve()

    def check(self, *, expected_version: str, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            SemVer.parse(expected_version)
        except (TypeError, ValueError):
            return False
        deadline = time.monotonic() + timeout_seconds
        try:
            version = subprocess.run(
                [str(self.executable), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, deadline - time.monotonic()),
                check=False,
                shell=False,
            )
            expected_output = f"{self.executable.name} {expected_version}".encode("utf-8")
            if (
                version.returncode != 0
                or len(version.stdout) > 512
                or version.stdout.strip() != expected_output
            ):
                return False
            completed = subprocess.run(
                [str(self.executable), "--smoke-test", "--no-browser"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, deadline - time.monotonic()),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


class UpdateRuntimeService:
    """Expose one authenticated manifest as consent-gated product operations."""

    def __init__(
        self,
        *,
        source: AuthenticatedUpdateSource,
        parser: ManifestParser,
        engine: UpdateEngine,
        installation_available: bool = True,
        task_runner: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.source = source
        self.parser = parser
        self.engine = engine
        self.installation_available = installation_available
        self.manifest_path = self.engine.cache_dir / "authenticated-manifest-v1.json"
        self.runtime_status_path = self.engine.cache_dir / "update-runtime-status-v1.json"
        self._task_runner = task_runner or self._start_thread
        self._lock = threading.RLock()
        self._active_operation: str | None = None
        self._last_error_code = self._load_last_error_code()

    def check(self, *, prepare: bool = True) -> UpdateCheckResult:
        with self._lock:
            self._assert_idle()
        result = self.source.check()
        if not prepare:
            return result
        if result.status is not UpdateStatus.AVAILABLE or result.manifest is None:
            return result
        manifest = result.manifest
        with self._lock:
            journal = self._journal_optional()
            if journal is not None and journal.state in _NONTERMINAL_STATES:
                cached = self._authenticated_manifest()
                if _manifest_fingerprint(cached) != journal.manifest_fingerprint:
                    raise UpdateRuntimeError(
                        409,
                        "update_recovery_required",
                        "The authenticated update cache does not match its journal.",
                    )
                if _manifest_fingerprint(manifest) != journal.manifest_fingerprint:
                    return UpdateCheckResult(
                        UpdateStatus.AVAILABLE,
                        cached,
                        "an authenticated update is already in progress",
                    )
                return UpdateCheckResult(UpdateStatus.AVAILABLE, cached, result.detail)
            self._persist_manifest(manifest)
            try:
                self.engine.prepare(manifest)
            except (UpdateEngineError, OSError) as error:
                self._record_error(_error_code(error))
                raise _public_error(error) from None
            self._record_error(None)
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            operation = self._active_operation
            last_error = self._last_error_code
            try:
                journal = self._journal_optional()
            except UpdateRuntimeError:
                return {
                    "state": UpdateState.MANUAL_RECOVERY.value,
                    "operation": operation,
                    "version": None,
                    "channel": None,
                    "critical": False,
                    "release_notes_url": None,
                    "progress": {"phase": "manual-recovery", "completed_bytes": 0, "total_bytes": 0, "percent": 0},
                    "last_error_code": "update_journal_invalid",
                    "recovery_required": True,
                    "can_consent_download": False,
                    "can_download": False,
                    "can_consent_install": False,
                    "can_install": False,
                    "can_resume": False,
                    "installation_available": self.installation_available,
                    "installation_unavailable_code": None if self.installation_available else "installer_helper_unavailable",
                }
            if journal is None:
                return {
                    "state": "idle",
                    "operation": operation,
                    "version": None,
                    "channel": None,
                    "critical": False,
                    "release_notes_url": None,
                    "progress": {"phase": "idle", "completed_bytes": 0, "total_bytes": 0, "percent": 0},
                    "last_error_code": last_error,
                    "recovery_required": False,
                    "can_consent_download": False,
                    "can_download": False,
                    "can_consent_install": False,
                    "can_install": False,
                    "can_resume": False,
                    "installation_available": self.installation_available,
                    "installation_unavailable_code": None if self.installation_available else "installer_helper_unavailable",
                }
            release_notes_url = None
            try:
                manifest = self._authenticated_manifest()
                if _manifest_fingerprint(manifest) == journal.manifest_fingerprint:
                    release_notes_url = manifest.release_notes_url
            except UpdateRuntimeError:
                last_error = "authenticated_manifest_unavailable"
            progress = self._progress(journal)
            idle = operation is None
            return {
                "state": journal.state.value,
                "operation": operation,
                "version": journal.version,
                "channel": journal.channel,
                "critical": journal.critical,
                "release_notes_url": release_notes_url,
                "progress": progress,
                "last_error_code": last_error,
                "recovery_required": journal.state in _RECOVERY_STATES,
                "can_consent_download": idle and journal.state is UpdateState.AVAILABLE,
                "can_download": idle and journal.state is UpdateState.DOWNLOAD_CONSENTED,
                "can_consent_install": (
                    self.installation_available and idle and journal.state is UpdateState.STAGED
                ),
                "can_install": (
                    self.installation_available
                    and idle
                    and journal.state is UpdateState.INSTALL_CONSENTED
                ),
                "can_resume": (
                    idle
                    and (
                        journal.state in {UpdateState.DOWNLOADING, UpdateState.VERIFIED}
                        or (
                            self.installation_available
                            and journal.state in {
                                UpdateState.INSTALLING,
                                UpdateState.HEALTH_CHECK,
                            }
                        )
                    )
                ),
                "installation_available": self.installation_available,
                "installation_unavailable_code": (
                    None if self.installation_available else "installer_helper_unavailable"
                ),
            }

    def consent_download(self, *, version: str, confirm: bool) -> dict[str, Any]:
        self._require_confirmation(version=version, confirm=confirm)
        with self._lock:
            self._assert_idle()
            try:
                self.engine.consent_download()
            except (UpdateEngineError, OSError) as error:
                raise _public_error(error) from None
            self._record_error(None)
            return self.status()

    def start_download(self) -> dict[str, Any]:
        with self._lock:
            self._assert_idle()
            manifest = self._authenticated_manifest()
            journal = self._require_journal()
            if journal.state is not UpdateState.DOWNLOAD_CONSENTED:
                raise UpdateRuntimeError(409, "update_state_invalid", "Download consent is required first.")
            self._assert_manifest_matches(journal.manifest_fingerprint, manifest)
            self._begin("download", lambda: self.engine.download(manifest))
            return self.status()

    def consent_install(self, *, version: str, confirm: bool) -> dict[str, Any]:
        self._require_installation()
        self._require_confirmation(version=version, confirm=confirm)
        with self._lock:
            self._assert_idle()
            manifest = self._authenticated_manifest()
            try:
                self.engine.consent_install(manifest)
            except (UpdateEngineError, OSError) as error:
                raise _public_error(error) from None
            self._record_error(None)
            return self.status()

    def start_install(self) -> dict[str, Any]:
        self._require_installation()
        with self._lock:
            self._assert_idle()
            manifest = self._authenticated_manifest()
            journal = self._require_journal()
            if journal.state is not UpdateState.INSTALL_CONSENTED:
                raise UpdateRuntimeError(409, "update_state_invalid", "Install consent is required first.")
            self._assert_manifest_matches(journal.manifest_fingerprint, manifest)
            self._begin("install", lambda: self.engine.install(manifest))
            return self.status()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            self._assert_idle()
            manifest = self._authenticated_manifest()
            journal = self._require_journal()
            if (
                not self.installation_available
                and journal.state in {UpdateState.INSTALLING, UpdateState.HEALTH_CHECK}
            ):
                self._require_installation()
            self._assert_manifest_matches(journal.manifest_fingerprint, manifest)
            self._begin("resume", lambda: self.engine.resume(manifest))
            return self.status()

    def _require_confirmation(self, *, version: str, confirm: bool) -> None:
        if confirm is not True:
            raise UpdateRuntimeError(400, "update_consent_required", "Explicit update consent is required.")
        if not isinstance(version, str) or len(version) > 64:
            raise UpdateRuntimeError(400, "update_version_invalid", "The update version is invalid.")
        journal = self._require_journal()
        if version != journal.version:
            raise UpdateRuntimeError(409, "update_version_changed", "The update version changed before consent.")

    def _require_installation(self) -> None:
        if not self.installation_available:
            raise UpdateRuntimeError(
                503,
                "installer_helper_unavailable",
                "Automatic installation is unavailable for this package; the verified download was not executed.",
            )

    def _begin(self, operation: str, action: Callable[[], UpdateState]) -> None:
        self._active_operation = operation
        self._record_error(None)

        def execute() -> None:
            error_code: str | None = None
            try:
                final_state = action()
                if final_state is UpdateState.ROLLED_BACK:
                    error_code = "update_rolled_back"
                elif final_state is UpdateState.MANUAL_RECOVERY:
                    error_code = "manual_recovery_required"
            except Exception as error:
                error_code = _error_code(error)
                if operation == "download":
                    try:
                        manifest = self._authenticated_manifest()
                        self.engine.resume(manifest)
                    except Exception:
                        pass
            finally:
                with self._lock:
                    self._active_operation = None
                    self._record_error(error_code)

        try:
            self._task_runner(execute)
        except Exception as error:
            self._active_operation = None
            self._record_error(_error_code(error))
            raise UpdateRuntimeError(503, "update_task_unavailable", "The update task could not be started.") from None

    @staticmethod
    def _start_thread(action: Callable[[], None]) -> None:
        thread = threading.Thread(target=action, name="quota-monitor-update", daemon=True)
        thread.start()

    def _assert_idle(self) -> None:
        if self._active_operation is not None:
            raise UpdateRuntimeError(409, "update_busy", "Another update operation is still running.")

    def _journal_optional(self):
        if not self.engine.journal_path.exists():
            return None
        try:
            return self.engine.load_journal()
        except (UpdateEngineError, OSError):
            raise UpdateRuntimeError(409, "update_journal_invalid", "The update journal requires manual recovery.") from None

    def _require_journal(self):
        journal = self._journal_optional()
        if journal is None:
            raise UpdateRuntimeError(409, "update_not_available", "No authenticated update is prepared.")
        return journal

    def _authenticated_manifest(self) -> UpdateManifest:
        try:
            payload = self.manifest_path.read_bytes()
            return self.parser.parse(payload)
        except Exception:
            raise UpdateRuntimeError(
                409,
                "authenticated_manifest_unavailable",
                "The authenticated update manifest is unavailable.",
            ) from None

    def _persist_manifest(self, manifest: UpdateManifest) -> None:
        try:
            document = json.loads(manifest.signing_payload.decode("utf-8", errors="strict"))
            if not isinstance(document, dict) or "signature" in document:
                raise ValueError("invalid signing payload")
            document["signature"] = base64.b64encode(manifest.signature).decode("ascii")
            encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
            reparsed = self.parser.parse(encoded)
            if _manifest_fingerprint(reparsed) != _manifest_fingerprint(manifest):
                raise ValueError("manifest fingerprint changed")
            self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.manifest_path.with_name(self.manifest_path.name + ".tmp-" + uuid.uuid4().hex)
            try:
                with temporary.open("xb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.manifest_path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception:
            raise UpdateRuntimeError(503, "update_cache_unavailable", "The authenticated update could not be cached.") from None

    @staticmethod
    def _assert_manifest_matches(expected: str, manifest: UpdateManifest) -> None:
        if _manifest_fingerprint(manifest) != expected:
            raise UpdateRuntimeError(
                409,
                "update_manifest_changed",
                "The authenticated update manifest does not match the active journal.",
            )

    def _progress(self, journal) -> dict[str, Any]:
        total = journal.artifact_size
        if journal.state in {
            UpdateState.VERIFIED,
            UpdateState.STAGED,
            UpdateState.INSTALL_CONSENTED,
            UpdateState.INSTALLING,
            UpdateState.HEALTH_CHECK,
            UpdateState.COMMITTED,
        }:
            completed = total
        elif journal.state is UpdateState.DOWNLOADING:
            try:
                completed = min(total, (self.engine.cache_dir / journal.partial_name).stat().st_size)
            except OSError:
                completed = 0
        else:
            completed = 0
        percent = int((completed * 100) / total) if total else 0
        return {
            "phase": journal.state.value,
            "completed_bytes": completed,
            "total_bytes": total,
            "percent": min(100, percent),
        }

    def _load_last_error_code(self) -> str | None:
        try:
            payload = self.runtime_status_path.read_bytes()
            if len(payload) > 2048:
                return "update_status_invalid"
            document = json.loads(payload)
            value = document.get("last_error_code")
            if document.get("schema_version") != RUNTIME_STATUS_SCHEMA_VERSION:
                return "update_status_invalid"
            if value is not None and (not isinstance(value, str) or not _ERROR_CODE.fullmatch(value)):
                return "update_status_invalid"
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return "update_status_invalid"

    def _record_error(self, code: str | None) -> None:
        self._last_error_code = code
        try:
            self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.runtime_status_path.with_name(
                self.runtime_status_path.name + ".tmp-" + uuid.uuid4().hex
            )
            encoded = json.dumps(
                {"schema_version": RUNTIME_STATUS_SCHEMA_VERSION, "last_error_code": code},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                with temporary.open("xb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.runtime_status_path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            # Status persistence is diagnostic only; engine journal writes remain authoritative.
            pass


def load_product_update_trust(path: Path) -> ProductUpdateTrust:
    """Load an immutable, bundled public trust document with strict fields."""

    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("trust document is too large")
        document = json.loads(raw, object_pairs_hook=_unique_object)
        required = {
            "schema_version",
            "manifest_urls",
            "manifest_hosts",
            "artifact_hosts",
            "release_notes_hosts",
            "public_keys",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise ValueError("trust document fields are invalid")
        if document["schema_version"] != TRUST_SCHEMA_VERSION:
            raise ValueError("trust document schema is invalid")
        manifest_hosts = _host_set(document["manifest_hosts"], "manifest_hosts")
        artifact_hosts = _host_set(document["artifact_hosts"], "artifact_hosts")
        release_notes_hosts = _host_set(document["release_notes_hosts"], "release_notes_hosts")
        keyring = document["public_keys"]
        if not isinstance(keyring, dict) or not 1 <= len(keyring) <= 4:
            raise ValueError("public_keys must contain between one and four keys")
        public_keys = {}
        for key_id, value in keyring.items():
            if not isinstance(key_id, str) or not _TRUST_KEY_ID.fullmatch(key_id):
                raise ValueError("public_keys contains an invalid key ID")
            public_keys[key_id] = decode_base64_key(
                value, expected_bytes=32, label="public key"
            )
        build_ed25519_keyring(public_keys)
        manifest_urls_document = document["manifest_urls"]
        if not isinstance(manifest_urls_document, dict) or set(manifest_urls_document) != {
            "beta",
            "stable",
        }:
            raise ValueError("manifest_urls must contain exactly beta and stable")
        manifest_urls = {
            channel: validate_https_url(
                value,
                allowed_hosts=manifest_hosts,
                field=f"manifest_urls.{channel}",
            )
            for channel, value in manifest_urls_document.items()
        }
        ManifestPolicy(
            allowed_artifact_hosts=artifact_hosts,
            allowed_release_notes_hosts=release_notes_hosts,
        )
        return ProductUpdateTrust(
            manifest_urls=manifest_urls,
            manifest_hosts=manifest_hosts,
            artifact_hosts=artifact_hosts,
            release_notes_hosts=release_notes_hosts,
            public_keys=public_keys,
        )
    except Exception as error:
        raise ValueError("bundled update trust is invalid") from error


def build_product_update_runtime(
    *,
    paths: AppPaths,
    channel: str,
    current_version: str,
    trust_path: Path,
    target_path: Path | None = None,
    platform_os: str | None = None,
    platform_arch: str | None = None,
    artifact_format: str | None = None,
    task_runner: Callable[[Callable[[], None]], None] | None = None,
) -> UpdateRuntimeService | None:
    """Compose the production updater, or return unavailable for source builds.

    A caller may inject target/platform values for packaged-build tests.  Normal
    application startup enables installation only for a frozen Windows portable
    executable or Linux AppImage. macOS app-bundle replacement needs the signed
    external helper and is therefore fail-closed here.
    """

    if not Path(trust_path).is_file():
        return None
    trust = load_product_update_trust(trust_path)
    try:
        manifest_url = trust.manifest_urls[channel]
    except KeyError:
        raise ValueError("configured update channel has no bundled trusted URL") from None
    operating_system = platform_os or _platform_os()
    architecture = platform_arch or _platform_arch()
    selected_format = artifact_format
    selected_target = Path(target_path).resolve() if target_path is not None else None
    if selected_target is None:
        if not getattr(sys, "frozen", False):
            return None
        if operating_system == "windows":
            selected_target = Path(sys.executable).resolve()
            selected_format = selected_format or "portable-exe"
        elif operating_system == "linux" and os.environ.get("APPIMAGE"):
            appimage = Path(os.environ["APPIMAGE"])
            if not appimage.is_absolute():
                return None
            selected_target = appimage.resolve()
            selected_format = selected_format or "appimage"
        else:
            return None
    if not selected_target.is_file():
        return None
    if selected_format is None:
        raise ValueError("artifact_format is required for an injected update target")
    policy = ManifestPolicy(
        allowed_artifact_hosts=trust.artifact_hosts,
        allowed_release_notes_hosts=trust.release_notes_hosts,
    )
    parser = ManifestParser(policy=policy, verifier=build_ed25519_keyring(trust.public_keys))
    version = SemVer.parse(current_version)
    checker = UpdateChecker(
        parser=parser,
        current_version=version,
        updater_version=version,
        channel=channel,
    )
    source = RemoteAuthenticatedUpdateSource(
        checker=checker,
        manifest_url=manifest_url,
        fetcher=UrllibManifestFetcher(allowed_hosts=trust.manifest_hosts),
    )
    installation_available = operating_system == "linux" and selected_format == "appimage"
    installer = AtomicFileUpdateInstaller() if installation_available else FailClosedUpdateInstaller()
    engine = UpdateEngine(
        current_version=version,
        channel=channel,
        platform_os=operating_system,
        platform_arch=architecture,
        artifact_format=selected_format,
        cache_dir=paths.update_cache_dir,
        target_path=selected_target,
        downloader=HttpsArtifactDownloader(allowed_hosts=trust.artifact_hosts),
        installer=installer,
        health_checker=ExecutableSmokeHealthChecker(selected_target),
    )
    return UpdateRuntimeService(
        source=source,
        parser=parser,
        engine=engine,
        installation_available=installation_available,
        task_runner=task_runner,
    )


def _manifest_fingerprint(manifest: UpdateManifest) -> str:
    return hashlib.sha256(manifest.signing_payload + manifest.signature).hexdigest()


def _public_error(error: Exception) -> UpdateRuntimeError:
    code = _error_code(error)
    status = 507 if code == "insufficient_disk_space" else 409
    if code in {"artifact_download_failed", "update_failed"}:
        status = 502
    messages = {
        "insufficient_disk_space": "There is not enough disk space for this update.",
        "artifact_download_failed": "The authenticated update artifact could not be downloaded.",
        "update_policy_rejected": "The update no longer satisfies the active policy.",
        "update_state_invalid": "The update is not in the required state.",
        "update_failed": "The update operation failed safely.",
    }
    return UpdateRuntimeError(status, code, messages.get(code, messages["update_failed"]))


def _error_code(error: Exception) -> str:
    if isinstance(error, InsufficientDiskSpaceError):
        return "insufficient_disk_space"
    if isinstance(error, ArtifactDownloadError):
        return "artifact_download_failed"
    if isinstance(error, UpdatePolicyError):
        return "update_policy_rejected"
    if isinstance(error, UpdateStateError):
        return "update_state_invalid"
    return "update_failed"


def _host_set(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError(f"{field} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} contains an invalid host")
    return frozenset(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate trust document field")
        result[key] = value
    return result


def _platform_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _platform_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise ValueError("the current architecture is unsupported for updates")
