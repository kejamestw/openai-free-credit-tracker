"""Signed update-manifest parsing and a read-only update check service."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .canonical_json import CanonicalizationError, canonicalize_json
from .semver import SemVer, SemVerError


UPDATE_MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
SUPPORTED_CHANNELS = frozenset({"stable", "beta"})

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}$")
_SUPPORTED_OPERATING_SYSTEMS = frozenset({"windows", "macos", "linux"})


class UpdateManifestError(ValueError):
    """Base class for untrusted manifest failures."""


class ManifestValidationError(UpdateManifestError):
    pass


class ManifestSignatureError(UpdateManifestError):
    pass


class ManifestPolicyError(UpdateManifestError):
    pass


class URLValidationError(ManifestPolicyError):
    pass


class ArtifactVerificationError(UpdateManifestError):
    pass


class SignatureVerifier(Protocol):
    """Injected verifier; production implementations must perform Ed25519 verify."""

    def verify(self, *, key_id: str, message: bytes, signature: bytes) -> bool: ...


class Ed25519Backend(Protocol):
    """Minimal adapter boundary for cryptography, pynacl, or an OS backend."""

    def verify(
        self, *, public_key: bytes, message: bytes, signature: bytes
    ) -> bool | None: ...


@dataclass(frozen=True)
class Ed25519KeyringVerifier:
    """Resolve a public key by key ID and delegate raw Ed25519 verification."""

    public_keys: Mapping[str, bytes]
    backend: Ed25519Backend

    def verify(self, *, key_id: str, message: bytes, signature: bytes) -> bool:
        public_key = self.public_keys.get(key_id)
        if public_key is None or len(public_key) != 32 or len(signature) != 64:
            return False
        try:
            return self.backend.verify(
                public_key=public_key,
                message=message,
                signature=signature,
            ) is not False
        except Exception:
            return False


@dataclass(frozen=True)
class ArtifactDescriptor:
    url: str
    size: int
    sha256: str
    os: str | None = None
    arch: str | None = None
    format: str | None = None


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    channel: str
    version: SemVer
    published_at: datetime
    minimum_updater_version: SemVer
    artifacts: tuple[ArtifactDescriptor, ...]
    release_notes_url: str
    signature: bytes
    key_id: str
    signing_payload: bytes
    expires_at: datetime | None = None
    critical: bool = False

    @property
    def artifact(self) -> ArtifactDescriptor:
        """Compatibility accessor for the legacy single-artifact manifest."""

        if len(self.artifacts) != 1:
            raise ValueError("manifest has multiple artifacts; select a platform target")
        return self.artifacts[0]

    def artifacts_for(self, *, os: str, arch: str) -> tuple[ArtifactDescriptor, ...]:
        """Return explicit platform matches; legacy artifacts remain generic."""

        return tuple(
            artifact
            for artifact in self.artifacts
            if (artifact.os is None or artifact.os == os)
            and (artifact.arch is None or artifact.arch == arch)
        )


@dataclass(frozen=True)
class ManifestPolicy:
    allowed_artifact_hosts: frozenset[str]
    allowed_release_notes_hosts: frozenset[str] | None = None
    allowed_channels: frozenset[str] = SUPPORTED_CHANNELS
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES

    def __post_init__(self) -> None:
        artifact_hosts = _normalize_host_set(self.allowed_artifact_hosts)
        release_hosts = (
            artifact_hosts
            if self.allowed_release_notes_hosts is None
            else _normalize_host_set(self.allowed_release_notes_hosts)
        )
        channels = frozenset(self.allowed_channels)
        if not artifact_hosts:
            raise ValueError("at least one artifact host must be allowlisted")
        if not release_hosts:
            raise ValueError("at least one release-notes host must be allowlisted")
        if not channels or not channels <= SUPPORTED_CHANNELS:
            raise ValueError("allowed channels must be a non-empty supported subset")
        if type(self.max_manifest_bytes) is not int or self.max_manifest_bytes < 1:
            raise ValueError("max_manifest_bytes must be a positive integer")
        if type(self.max_artifact_bytes) is not int or self.max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be a positive integer")
        object.__setattr__(self, "allowed_artifact_hosts", artifact_hosts)
        object.__setattr__(self, "allowed_release_notes_hosts", release_hosts)
        object.__setattr__(self, "allowed_channels", channels)


class ManifestParser:
    """Parse, authenticate, and validate an update manifest without side effects."""

    def __init__(
        self,
        *,
        policy: ManifestPolicy,
        verifier: SignatureVerifier,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.verifier = verifier
        self._now = now or (lambda: datetime.now(timezone.utc))

    def parse(self, document: bytes | str | Mapping[str, Any]) -> UpdateManifest:
        root = self._decode(document)
        required = {
            "schema_version",
            "channel",
            "version",
            "published_at",
            "minimum_updater_version",
            "release_notes_url",
            "signature",
            "key_id",
        }
        optional = {"expires_at", "critical", "artifact", "artifacts"}
        _check_fields(root, required, optional, "manifest", allow_unknown=True)
        if ("artifact" in root) == ("artifacts" in root):
            raise ManifestValidationError(
                "manifest must contain exactly one of artifact or artifacts"
            )

        key_id = _text(root["key_id"], "key_id")
        if not _KEY_ID.fullmatch(key_id):
            raise ManifestValidationError("key_id has an invalid format")
        signature_text = _text(root["signature"], "signature")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise ManifestValidationError("signature must be strict base64") from error
        if len(signature) != 64:
            raise ManifestValidationError("Ed25519 signature must be 64 bytes")

        try:
            signing_payload = manifest_signing_payload(root)
        except CanonicalizationError as error:
            raise ManifestValidationError(str(error)) from error
        try:
            verified = self.verifier.verify(
                key_id=key_id,
                message=signing_payload,
                signature=signature,
            )
        except Exception as error:
            raise ManifestSignatureError("manifest signature verification failed") from error
        if verified is not True:
            raise ManifestSignatureError("manifest signature verification failed")

        schema_version = _integer(root["schema_version"], "schema_version")
        if schema_version != UPDATE_MANIFEST_SCHEMA_VERSION:
            raise ManifestPolicyError(
                f"unsupported manifest schema version: {schema_version}"
            )
        channel = _text(root["channel"], "channel")
        if channel not in self.policy.allowed_channels:
            raise ManifestPolicyError(f"unsupported update channel: {channel}")
        version = _semver(root["version"], "version")
        if channel == "stable" and version.is_prerelease:
            raise ManifestPolicyError("stable channel must not contain a pre-release version")
        if channel == "beta" and not version.is_prerelease:
            raise ManifestPolicyError("beta channel requires a pre-release version")
        minimum_updater_version = _semver(
            root["minimum_updater_version"], "minimum_updater_version"
        )
        published_at = _timestamp(root["published_at"], "published_at")
        expires_at = (
            _timestamp(root["expires_at"], "expires_at")
            if "expires_at" in root
            else None
        )
        if expires_at is not None:
            if expires_at <= published_at:
                raise ManifestPolicyError("expires_at must be later than published_at")
            now = self._now()
            if now.tzinfo is None:
                raise RuntimeError("manifest clock must return a timezone-aware datetime")
            if expires_at <= now:
                raise ManifestPolicyError("manifest has expired")

        if "artifact" in root:
            artifacts = (self._parse_artifact(root["artifact"], "artifact", legacy=True),)
        else:
            artifact_values = root["artifacts"]
            if not isinstance(artifact_values, list) or not artifact_values:
                raise ManifestValidationError("artifacts must be a non-empty array")
            if len(artifact_values) > 32:
                raise ManifestPolicyError("artifacts contains too many entries")
            artifacts = tuple(
                self._parse_artifact(value, f"artifacts[{index}]", legacy=False)
                for index, value in enumerate(artifact_values)
            )
            targets = [(item.os, item.arch, item.format) for item in artifacts]
            if len(targets) != len(set(targets)):
                raise ManifestValidationError("artifacts contains a duplicate platform target")
        release_notes_url = validate_https_url(
            _text(root["release_notes_url"], "release_notes_url"),
            allowed_hosts=self.policy.allowed_release_notes_hosts or frozenset(),
            field="release_notes_url",
        )

        critical = _boolean(root.get("critical", False), "critical")

        return UpdateManifest(
            schema_version=schema_version,
            channel=channel,
            version=version,
            published_at=published_at,
            minimum_updater_version=minimum_updater_version,
            artifacts=artifacts,
            release_notes_url=release_notes_url,
            signature=signature,
            key_id=key_id,
            signing_payload=signing_payload,
            expires_at=expires_at,
            critical=critical,
        )

    def _parse_artifact(
        self, value: Any, field: str, *, legacy: bool
    ) -> ArtifactDescriptor:
        artifact = _object(value, field)
        platform_fields = set() if legacy else {"os", "arch", "format"}
        _check_fields(
            artifact,
            {"url", "size", "sha256"} | platform_fields,
            set(),
            field,
            allow_unknown=True,
        )
        artifact_url = validate_https_url(
            _text(artifact["url"], f"{field}.url"),
            allowed_hosts=self.policy.allowed_artifact_hosts,
            field=f"{field}.url",
        )
        artifact_size = _integer(artifact["size"], f"{field}.size")
        if not 1 <= artifact_size <= self.policy.max_artifact_bytes:
            raise ManifestPolicyError(
                f"{field}.size must be between 1 and {self.policy.max_artifact_bytes}"
            )
        artifact_hash = _text(artifact["sha256"], f"{field}.sha256")
        if not _SHA256.fullmatch(artifact_hash):
            raise ManifestValidationError(
                f"{field}.sha256 must contain 64 hex characters"
            )
        if legacy:
            operating_system = architecture = artifact_format = None
        else:
            operating_system = _artifact_token(artifact["os"], f"{field}.os")
            if operating_system not in _SUPPORTED_OPERATING_SYSTEMS:
                raise ManifestPolicyError(f"{field}.os is not supported")
            architecture = _artifact_token(artifact["arch"], f"{field}.arch")
            artifact_format = _artifact_token(artifact["format"], f"{field}.format")
        return ArtifactDescriptor(
            url=artifact_url,
            size=artifact_size,
            sha256=artifact_hash.lower(),
            os=operating_system,
            arch=architecture,
            format=artifact_format,
        )

    def _decode(self, document: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
        if isinstance(document, bytes):
            if len(document) > self.policy.max_manifest_bytes:
                raise ManifestValidationError("manifest exceeds the maximum size")
            try:
                text = document.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ManifestValidationError("manifest must be UTF-8") from error
            return _load_json(text)
        if isinstance(document, str):
            if len(document.encode("utf-8")) > self.policy.max_manifest_bytes:
                raise ManifestValidationError("manifest exceeds the maximum size")
            return _load_json(document)
        return _object(document, "manifest")


def manifest_signing_payload(manifest: Mapping[str, Any]) -> bytes:
    """Canonicalize every top-level manifest field except ``signature``."""

    if "signature" not in manifest:
        raise ManifestValidationError("manifest is missing signature")
    return canonicalize_json({key: value for key, value in manifest.items() if key != "signature"})


def validate_https_url(
    value: str,
    *,
    allowed_hosts: frozenset[str] | set[str],
    field: str = "url",
) -> str:
    """Validate an HTTPS URL against an exact, IDNA-normalized host allowlist."""

    if any(character.isspace() or ord(character) < 32 for character in value):
        raise URLValidationError(f"{field} contains whitespace or control characters")
    if "\\" in value:
        raise URLValidationError(f"{field} must not contain backslashes")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise URLValidationError(f"{field} is not a valid URL") from error
    if parsed.scheme.lower() != "https":
        raise URLValidationError(f"{field} must use HTTPS")
    if not parsed.hostname or not parsed.netloc:
        raise URLValidationError(f"{field} must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError(f"{field} must not include credentials")
    if parsed.fragment:
        raise URLValidationError(f"{field} must not include a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise URLValidationError(f"{field} has an invalid port")
    host = _normalize_host(parsed.hostname)
    normalized_allowlist = _normalize_host_set(allowed_hosts)
    if host not in normalized_allowlist:
        raise URLValidationError(f"{field} host is not allowlisted: {host}")
    return value


class UpdateStatus(str, Enum):
    AVAILABLE = "available"
    UP_TO_DATE = "up-to-date"
    IGNORED_CHANNEL = "ignored-channel"
    REJECTED_DOWNGRADE = "rejected-downgrade"
    INCOMPATIBLE_UPDATER = "incompatible-updater"
    INVALID_MANIFEST = "invalid-manifest"
    NETWORK_ERROR = "network-error"


@dataclass(frozen=True)
class UpdateCheckResult:
    status: UpdateStatus
    manifest: UpdateManifest | None = None
    detail: str | None = None


class ManifestFetcher(Protocol):
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> bytes: ...


@dataclass(frozen=True)
class UpdateChecker:
    """Evaluate signed update metadata; never download or execute an artifact."""

    parser: ManifestParser
    current_version: SemVer
    updater_version: SemVer
    channel: str

    def __post_init__(self) -> None:
        if self.channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported update channel: {self.channel}")
        if not isinstance(self.current_version, SemVer):
            raise TypeError("current_version must be a SemVer")
        if not isinstance(self.updater_version, SemVer):
            raise TypeError("updater_version must be a SemVer")

    def check_document(self, document: bytes | str | Mapping[str, Any]) -> UpdateCheckResult:
        return self.evaluate(self.parser.parse(document))

    def evaluate(self, manifest: UpdateManifest) -> UpdateCheckResult:
        if manifest.channel != self.channel:
            return UpdateCheckResult(UpdateStatus.IGNORED_CHANNEL, manifest)
        if manifest.version < self.current_version:
            return UpdateCheckResult(UpdateStatus.REJECTED_DOWNGRADE, manifest)
        if manifest.version == self.current_version:
            return UpdateCheckResult(UpdateStatus.UP_TO_DATE, manifest)
        if manifest.minimum_updater_version > self.updater_version:
            return UpdateCheckResult(UpdateStatus.INCOMPATIBLE_UPDATER, manifest)
        return UpdateCheckResult(UpdateStatus.AVAILABLE, manifest)

    def check_remote(
        self,
        url: str,
        *,
        fetcher: ManifestFetcher,
        timeout_seconds: float = 10.0,
    ) -> UpdateCheckResult:
        try:
            document = fetcher.fetch(
                url,
                timeout_seconds=timeout_seconds,
                max_bytes=self.parser.policy.max_manifest_bytes,
            )
        except UpdateManifestError as error:
            return UpdateCheckResult(
                UpdateStatus.INVALID_MANIFEST,
                detail=f"update manifest was rejected: {type(error).__name__}",
            )
        except Exception as error:
            return UpdateCheckResult(
                UpdateStatus.NETWORK_ERROR,
                detail=f"update manifest could not be fetched: {type(error).__name__}",
            )
        try:
            return self.check_document(document)
        except UpdateManifestError as error:
            return UpdateCheckResult(
                UpdateStatus.INVALID_MANIFEST,
                detail=f"update manifest was rejected: {type(error).__name__}",
            )


class UrllibManifestFetcher:
    """Bounded HTTPS fetcher that revalidates every redirect destination."""

    def __init__(self, *, allowed_hosts: frozenset[str] | set[str]) -> None:
        self.allowed_hosts = _normalize_host_set(allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("at least one manifest host must be allowlisted")
        self._opener = urllib.request.build_opener(
            _AllowlistedRedirectHandler(self.allowed_hosts)
        )

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> bytes:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        validate_https_url(url, allowed_hosts=self.allowed_hosts, field="manifest_url")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": "OpenAI-Free-Credit-Tracker",
            },
            method="GET",
        )
        with self._opener.open(request, timeout=timeout_seconds) as response:
            validate_https_url(
                response.geturl(),
                allowed_hosts=self.allowed_hosts,
                field="manifest_redirect_url",
            )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    announced_size = int(content_length)
                except ValueError as error:
                    raise ManifestValidationError("invalid Content-Length") from error
                if announced_size < 0:
                    raise ManifestValidationError("invalid Content-Length")
                if announced_size > max_bytes:
                    raise ManifestValidationError("manifest exceeds the maximum size")
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ManifestValidationError("manifest exceeds the maximum size")
        return payload


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_https_url(
            newurl,
            allowed_hosts=self.allowed_hosts,
            field="manifest_redirect_url",
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify_artifact_stream(
    stream: BinaryIO,
    descriptor: ArtifactDescriptor,
    *,
    chunk_size: int = 1024 * 1024,
) -> None:
    """Verify an already-downloaded artifact without executing or moving it."""

    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("artifact stream must be opened in binary mode")
        size += len(chunk)
        if size > descriptor.size:
            raise ArtifactVerificationError("artifact size does not match manifest")
        digest.update(chunk)
    if size != descriptor.size:
        raise ArtifactVerificationError("artifact size does not match manifest")
    if digest.hexdigest() != descriptor.sha256.lower():
        raise ArtifactVerificationError("artifact SHA-256 does not match manifest")


def verify_artifact_file(path: Path | str, descriptor: ArtifactDescriptor) -> None:
    with Path(path).open("rb") as stream:
        verify_artifact_stream(stream, descriptor)


def _load_json(value: str) -> Mapping[str, Any]:
    try:
        document = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ManifestValidationError("manifest is not valid JSON") from error
    return _object(document, "manifest")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ManifestValidationError(f"non-standard JSON number is not allowed: {value}")


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ManifestValidationError(f"{field} field names must be strings")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise ManifestValidationError(f"{field} must be an integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ManifestValidationError(f"{field} must be a boolean")
    return value


def _artifact_token(value: Any, field: str) -> str:
    token = _text(value, field)
    if not _ARTIFACT_TOKEN.fullmatch(token):
        raise ManifestValidationError(f"{field} has an invalid format")
    return token


def _semver(value: Any, field: str) -> SemVer:
    text = _text(value, field)
    try:
        return SemVer.parse(text)
    except SemVerError as error:
        raise ManifestValidationError(f"{field} must be a valid SemVer") from error


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestValidationError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _check_fields(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str],
    field: str,
    *,
    allow_unknown: bool = False,
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise ManifestValidationError(f"{field} is missing fields: {', '.join(missing)}")
    if unknown and not allow_unknown:
        raise ManifestValidationError(f"{field} has unknown fields: {', '.join(unknown)}")


def _normalize_host_set(hosts: frozenset[str] | set[str]) -> frozenset[str]:
    try:
        return frozenset(_normalize_host(host) for host in hosts)
    except TypeError as error:
        raise ValueError("host allowlist must contain strings") from error


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("allowlisted host must be a non-empty string")
    candidate = host.rstrip(".").lower()
    if "://" in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError("allowlist entries must be host names, not URLs")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError(f"invalid host name: {host}") from error
