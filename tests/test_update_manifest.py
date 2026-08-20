import base64
import copy
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest

from quota_monitor.semver import SemVer
from quota_monitor.update_manifest import (
    ArtifactDescriptor,
    ArtifactVerificationError,
    Ed25519KeyringVerifier,
    ManifestParser,
    ManifestPolicy,
    ManifestPolicyError,
    ManifestSignatureError,
    ManifestValidationError,
    URLValidationError,
    UpdateChecker,
    UpdateStatus,
    UrllibManifestFetcher,
    manifest_signing_payload,
    validate_https_url,
    verify_artifact_stream,
)


NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


class TestVerifier:
    @staticmethod
    def signature(key_id, message):
        return hashlib.sha512(key_id.encode("ascii") + message).digest()

    def verify(self, *, key_id, message, signature):
        return signature == self.signature(key_id, message)


def manifest_policy(max_artifact_bytes=1024 * 1024):
    return ManifestPolicy(
        allowed_artifact_hosts=frozenset({"downloads.example.com"}),
        allowed_release_notes_hosts=frozenset({"releases.example.com"}),
        max_artifact_bytes=max_artifact_bytes,
    )


def parser(max_artifact_bytes=1024 * 1024):
    return ManifestParser(
        policy=manifest_policy(max_artifact_bytes),
        verifier=TestVerifier(),
        now=lambda: NOW,
    )


def unsigned_manifest(**changes):
    document = {
        "schema_version": 1,
        "channel": "stable",
        "version": "0.2.1",
        "published_at": "2026-08-01T00:00:00Z",
        "minimum_updater_version": "0.2.0",
        "artifact": {
            "url": "https://downloads.example.com/releases/tracker.exe",
            "size": 7,
            "sha256": hashlib.sha256(b"release").hexdigest(),
        },
        "release_notes_url": "https://releases.example.com/v0.2.1",
        "signature": "",
        "key_id": "release-key-1",
    }
    document.update(changes)
    return document


def sign(document):
    signed = copy.deepcopy(document)
    signature = TestVerifier.signature(
        signed["key_id"], manifest_signing_payload(signed)
    )
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def test_parser_authenticates_and_returns_typed_manifest():
    parsed = parser().parse(json.dumps(sign(unsigned_manifest())).encode("utf-8"))

    assert parsed.version == SemVer.parse("0.2.1")
    assert parsed.minimum_updater_version == SemVer.parse("0.2.0")
    assert parsed.published_at == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert parsed.artifact.sha256 == hashlib.sha256(b"release").hexdigest()
    assert parsed.key_id == "release-key-1"
    assert b'"signature"' not in parsed.signing_payload
    assert b'"key_id":"release-key-1"' in parsed.signing_payload
    assert parsed.critical is False
    assert parsed.artifacts == (parsed.artifact,)
    assert parsed.artifact.os is None


def test_parser_normalizes_signed_platform_artifacts_and_critical_flag():
    artifact_hash = hashlib.sha256(b"release").hexdigest()
    document = unsigned_manifest(critical=True)
    document.pop("artifact")
    document["artifacts"] = [
        {
            "os": "windows",
            "arch": "x86_64",
            "format": "portable",
            "url": "https://downloads.example.com/releases/tracker.exe",
            "size": 7,
            "sha256": artifact_hash,
        },
        {
            "os": "windows",
            "arch": "arm64",
            "format": "installer",
            "url": "https://downloads.example.com/releases/tracker-arm64.exe",
            "size": 7,
            "sha256": artifact_hash,
        },
    ]

    parsed = parser().parse(sign(document))

    assert parsed.critical is True
    assert len(parsed.artifacts) == 2
    assert parsed.artifacts_for(os="windows", arch="arm64") == (parsed.artifacts[1],)
    assert parsed.artifacts_for(os="linux", arch="x86_64") == ()
    assert b'"critical":true' in parsed.signing_payload
    assert b'"artifacts"' in parsed.signing_payload
    with pytest.raises(ValueError, match="multiple artifacts"):
        _ = parsed.artifact


def test_same_schema_optional_fields_are_authenticated_then_ignored_for_compatibility():
    document = unsigned_manifest()
    document["future_policy"] = {"label": "signed extension"}
    document["artifact"]["future_format_detail"] = "signed extension"

    parsed = parser().parse(sign(document))

    assert parsed.version == SemVer.parse("0.2.1")
    assert b'"future_policy"' in parsed.signing_payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "0.2.2"),
        ("channel", "beta"),
        ("artifact.url", "https://downloads.example.com/releases/other.exe"),
        ("artifact.sha256", "0" * 64),
    ],
)
def test_any_signed_field_tampering_is_rejected_before_use(field, value):
    document = sign(unsigned_manifest())
    if field.startswith("artifact."):
        document["artifact"][field.split(".")[1]] = value
    else:
        document[field] = value

    with pytest.raises(ManifestSignatureError):
        parser().parse(document)


def test_key_id_is_signed_and_wrong_key_is_rejected():
    document = sign(unsigned_manifest())
    document["key_id"] = "release-key-2"

    with pytest.raises(ManifestSignatureError):
        parser().parse(document)


def test_expired_signed_manifest_is_rejected():
    document = unsigned_manifest(expires_at="2026-08-08T00:00:00Z")

    with pytest.raises(ManifestPolicyError, match="expired"):
        parser().parse(sign(document))


@pytest.mark.parametrize(
    ("mutate", "error", "message"),
    [
        (
            lambda document: document["artifact"].update(
                url="http://downloads.example.com/release.exe"
            ),
            URLValidationError,
            "HTTPS",
        ),
        (
            lambda document: document["artifact"].update(
                url="https://evil.example/release.exe"
            ),
            URLValidationError,
            "not allowlisted",
        ),
        (
            lambda document: document["artifact"].update(size=1_048_577),
            ManifestPolicyError,
            "between 1",
        ),
        (
            lambda document: document["artifact"].update(sha256="not-a-hash"),
            ManifestValidationError,
            "64 hex",
        ),
        (
            lambda document: document.update(version="0.3.0-rc.1"),
            ManifestPolicyError,
            "stable channel",
        ),
        (
            lambda document: document.update(schema_version=2),
            ManifestPolicyError,
            "unsupported manifest schema",
        ),
    ],
)
def test_validly_signed_but_disallowed_metadata_is_rejected(mutate, error, message):
    document = unsigned_manifest()
    mutate(document)

    with pytest.raises(error, match=message):
        parser().parse(sign(document))


def test_beta_channel_requires_an_explicit_prerelease():
    with pytest.raises(ManifestPolicyError, match="beta channel requires"):
        parser().parse(sign(unsigned_manifest(channel="beta", version="0.3.0")))


def test_manifest_rejects_ambiguous_or_duplicate_artifact_shapes():
    both = unsigned_manifest()
    both["artifacts"] = []
    with pytest.raises(ManifestValidationError, match="exactly one"):
        parser().parse(sign(both))

    duplicate = unsigned_manifest()
    legacy = duplicate.pop("artifact")
    platform_artifact = {
        "os": "windows",
        "arch": "x86_64",
        "format": "portable",
        **legacy,
    }
    duplicate["artifacts"] = [platform_artifact, copy.deepcopy(platform_artifact)]
    with pytest.raises(ManifestValidationError, match="duplicate platform target"):
        parser().parse(sign(duplicate))


@pytest.mark.parametrize(
    "url",
    [
        "http://downloads.example.com/release.exe",
        "https://user:pass@downloads.example.com/release.exe",
        "https://downloads.example.com/release.exe#fragment",
        "https://downloads.example.com\\@evil.example/release.exe",
        "https://evil.example/release.exe",
    ],
)
def test_url_policy_rejects_unsafe_or_non_allowlisted_urls(url):
    with pytest.raises(URLValidationError):
        validate_https_url(
            url,
            allowed_hosts=frozenset({"downloads.example.com"}),
        )


def test_url_policy_normalizes_idna_and_trailing_dns_dot():
    assert validate_https_url(
        "https://downloads.example.com./release.exe",
        allowed_hosts=frozenset({"DOWNLOADS.EXAMPLE.COM"}),
    ).endswith("/release.exe")


@pytest.mark.parametrize(
    ("version", "channel", "minimum", "status"),
    [
        ("0.2.1", "stable", "0.2.0", UpdateStatus.AVAILABLE),
        ("0.2.0", "stable", "0.2.0", UpdateStatus.UP_TO_DATE),
        ("0.1.9", "stable", "0.1.0", UpdateStatus.REJECTED_DOWNGRADE),
        ("0.2.1-beta.1", "beta", "0.2.0", UpdateStatus.IGNORED_CHANNEL),
        ("0.2.1", "stable", "0.3.0", UpdateStatus.INCOMPATIBLE_UPDATER),
    ],
)
def test_read_only_checker_enforces_version_channel_and_updater_policy(
    version, channel, minimum, status
):
    checker = UpdateChecker(
        parser=parser(),
        current_version=SemVer.parse("0.2.0"),
        updater_version=SemVer.parse("0.2.0"),
        channel="stable",
    )
    document = unsigned_manifest(
        version=version,
        channel=channel,
        minimum_updater_version=minimum,
    )

    assert checker.check_document(sign(document)).status is status


def test_consolidated_rc1_updater_accepts_an_rc2_manifest():
    checker = UpdateChecker(
        parser=parser(),
        current_version=SemVer.parse("1.0.0-rc.1"),
        updater_version=SemVer.parse("1.0.0-rc.1"),
        channel="beta",
    )
    document = unsigned_manifest(
        version="1.0.0-rc.2",
        channel="beta",
        minimum_updater_version="1.0.0-rc.1",
    )

    assert checker.check_document(sign(document)).status is UpdateStatus.AVAILABLE


class FailingFetcher:
    def fetch(self, url, *, timeout_seconds, max_bytes):
        raise TimeoutError("offline")


class StaticFetcher:
    def __init__(self, document):
        self.document = document

    def fetch(self, url, *, timeout_seconds, max_bytes):
        return self.document


def test_remote_check_contains_network_and_security_failures():
    checker = UpdateChecker(
        parser=parser(),
        current_version=SemVer.parse("0.2.0"),
        updater_version=SemVer.parse("0.2.0"),
        channel="stable",
    )

    offline = checker.check_remote("https://manifest.example", fetcher=FailingFetcher())
    invalid = checker.check_remote(
        "https://manifest.example",
        fetcher=StaticFetcher(b"not-json"),
    )

    assert offline.status is UpdateStatus.NETWORK_ERROR
    assert offline.manifest is None
    assert "offline" not in (offline.detail or "")
    assert invalid.status is UpdateStatus.INVALID_MANIFEST


class FakeResponse:
    def __init__(self, *, final_url, payload, content_length=None):
        self.final_url = final_url
        self.payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self.final_url

    def read(self, maximum):
        return self.payload[:maximum]


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


def test_manifest_fetcher_revalidates_final_redirect_host_without_network():
    fetcher = UrllibManifestFetcher(allowed_hosts={"manifest.example.com"})
    fetcher._opener = FakeOpener(
        FakeResponse(
            final_url="https://evil.example/manifest.json",
            payload=b"{}",
        )
    )

    with pytest.raises(URLValidationError, match="not allowlisted"):
        fetcher.fetch(
            "https://manifest.example.com/manifest.json",
            timeout_seconds=5,
            max_bytes=1024,
        )


def test_manifest_fetcher_enforces_announced_and_actual_size_without_network():
    fetcher = UrllibManifestFetcher(allowed_hosts={"manifest.example.com"})
    fetcher._opener = FakeOpener(
        FakeResponse(
            final_url="https://manifest.example.com/manifest.json",
            payload=b"{}",
            content_length="2000",
        )
    )
    with pytest.raises(ManifestValidationError, match="maximum size"):
        fetcher.fetch(
            "https://manifest.example.com/manifest.json",
            timeout_seconds=5,
            max_bytes=1024,
        )

    fetcher._opener = FakeOpener(
        FakeResponse(
            final_url="https://manifest.example.com/manifest.json",
            payload=b"x" * 1025,
        )
    )
    with pytest.raises(ManifestValidationError, match="maximum size"):
        fetcher.fetch(
            "https://manifest.example.com/manifest.json",
            timeout_seconds=5,
            max_bytes=1024,
        )


def test_manifest_fetcher_revalidates_mutable_signed_channel_without_cached_bytes():
    fetcher = UrllibManifestFetcher(allowed_hosts={"manifest.example.com"})
    opener = FakeOpener(
        FakeResponse(
            final_url="https://manifest.example.com/manifest.json",
            payload=b"{}",
        )
    )
    fetcher._opener = opener

    assert fetcher.fetch(
        "https://manifest.example.com/manifest.json",
        timeout_seconds=5,
        max_bytes=1024,
    ) == b"{}"
    request, timeout = opener.requests[0]
    assert request.get_header("Cache-control") == "no-cache"
    assert request.get_header("Pragma") == "no-cache"
    assert timeout == 5

def test_artifact_stream_checks_size_and_sha256_before_later_use():
    descriptor = ArtifactDescriptor(
        url="https://downloads.example.com/release.exe",
        size=7,
        sha256=hashlib.sha256(b"release").hexdigest(),
    )

    verify_artifact_stream(io.BytesIO(b"release"), descriptor, chunk_size=2)
    with pytest.raises(ArtifactVerificationError, match="size"):
        verify_artifact_stream(io.BytesIO(b"releas"), descriptor)
    with pytest.raises(ArtifactVerificationError, match="SHA-256"):
        verify_artifact_stream(io.BytesIO(b"changed"), descriptor)


class RecordingBackend:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def verify(self, *, public_key, message, signature):
        self.calls.append((public_key, message, signature))
        return self.result


def test_ed25519_keyring_supports_key_rotation_and_rejects_unknown_keys():
    backend = RecordingBackend()
    verifier = Ed25519KeyringVerifier(
        public_keys={"old": b"o" * 32, "current": b"c" * 32},
        backend=backend,
    )

    assert verifier.verify(
        key_id="current", message=b"payload", signature=b"s" * 64
    )
    assert backend.calls == [(b"c" * 32, b"payload", b"s" * 64)]
    assert not verifier.verify(
        key_id="unknown", message=b"payload", signature=b"s" * 64
    )


def test_duplicate_json_keys_and_oversized_manifest_are_rejected():
    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ManifestValidationError, match="duplicate JSON field"):
        parser().parse(duplicate)

    oversized = b"{" + b" " * (parser().policy.max_manifest_bytes + 1)
    with pytest.raises(ManifestValidationError, match="maximum size"):
        parser().parse(oversized)
