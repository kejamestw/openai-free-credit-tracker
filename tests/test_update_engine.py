import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quota_monitor.platform_adapters.update_install import (
    AtomicFileUpdateInstaller,
    WindowsHelperUpdateInstaller,
)
from quota_monitor.semver import SemVer
from quota_monitor.update_crypto import Ed25519ManifestSigner, build_ed25519_keyring
from quota_monitor.update_engine import (
    ArtifactDownloadError,
    HttpsArtifactDownloader,
    InsufficientDiskSpaceError,
    UpdateEngine,
    UpdatePolicyError,
    UpdateState,
)
from quota_monitor.update_manifest import ManifestParser, ManifestPolicy, URLValidationError


FIXTURES = Path(__file__).parent / "fixtures" / "update"
NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)
PAYLOAD = b"release"


def load_document():
    return json.loads((FIXTURES / "stable-windows.json").read_text(encoding="utf-8"))


def load_case(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def signed_manifest(change=None):
    document = load_document()
    if change:
        change(document)
    signer = Ed25519ManifestSigner.from_private_bytes(os.urandom(32))
    signed = signer.sign(document, key_id="fixture-key")
    return ManifestParser(
        policy=ManifestPolicy(
            allowed_artifact_hosts={"downloads.example.com"},
            allowed_release_notes_hosts={"releases.example.com"},
        ),
        verifier=build_ed25519_keyring({"fixture-key": signer.public_key_bytes}),
        now=lambda: NOW,
    ).parse(signed)


class StaticDownloader:
    def __init__(self, payload=PAYLOAD):
        self.payload = payload
        self.calls = 0

    def download(self, artifact, destination, *, timeout_seconds):
        self.calls += 1
        destination.write_bytes(self.payload)


class Health:
    def __init__(self, result=True):
        self.result = result

    def check(self, *, expected_version, timeout_seconds):
        return self.result


def engine(tmp_path, *, downloader=None, installer=None, health=None, **changes):
    target = tmp_path / "install" / "tracker.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    return UpdateEngine(
        current_version=SemVer.parse("0.2.1"), channel=changes.get("channel", "stable"),
        platform_os=changes.get("platform_os", "windows"), platform_arch="x86_64",
        artifact_format="portable", cache_dir=tmp_path / "cache", target_path=target,
        downloader=downloader or StaticDownloader(),
        installer=installer or AtomicFileUpdateInstaller(), health_checker=health or Health(),
        now=lambda: NOW,
    )


def test_state_machine_requires_both_consents_and_retains_rollback_copy(tmp_path):
    manifest = signed_manifest()
    updater = engine(tmp_path)
    assert updater.prepare(manifest) is UpdateState.AVAILABLE
    assert updater.load_journal().critical is False
    updater.consent_download()
    assert updater.download(manifest) is UpdateState.STAGED
    updater.consent_install(manifest)
    assert updater.install(manifest) is UpdateState.COMMITTED
    assert updater.target_path.read_bytes() == PAYLOAD
    backup = updater.target_path.parent / updater.load_journal().backup_name
    assert backup.read_bytes() == b"old-version"
    journal_text = updater.journal_path.read_text(encoding="utf-8")
    assert "https://" not in journal_text


def test_critical_update_still_requires_download_and_install_consent(tmp_path):
    manifest = signed_manifest(lambda value: value.update(critical=True))
    updater = engine(tmp_path)
    updater.prepare(manifest)
    assert updater.load_journal().critical is True
    with pytest.raises(Exception, match="download-consented"):
        updater.download(manifest)
    updater.consent_download()
    updater.download(manifest)
    with pytest.raises(Exception, match="install-consented"):
        updater.install(manifest)


def test_channel_platform_and_expiry_are_rechecked_at_execution_time(tmp_path):
    beta_case = load_case("wrong-channel.json")
    beta = signed_manifest(
        lambda value: value.update(
            channel=beta_case["channel"], version=beta_case["version"]
        )
    )
    with pytest.raises(UpdatePolicyError, match="channel"):
        engine(tmp_path / "beta").prepare(beta)

    platform_case = load_case("wrong-platform.json")
    linux = signed_manifest(
        lambda value: value["artifacts"][0].update(os=platform_case["os"])
    )
    with pytest.raises(UpdatePolicyError, match="platform"):
        engine(tmp_path / "linux").prepare(linux)

    expiry_case = load_case("expired.json")
    expired = signed_manifest(
        lambda value: value.update(expires_at="2026-08-10T00:00:00Z")
    )
    late = engine(tmp_path / "expired")
    late._now = lambda: datetime(2026, 8, 11, tzinfo=timezone.utc)
    with pytest.raises(UpdatePolicyError, match=expiry_case["expected"]):
        late.prepare(expired)


def test_failed_health_check_rolls_back_and_keeps_config_data_out_of_scope(tmp_path):
    manifest = signed_manifest()
    config = tmp_path / "config" / "config.json"
    data = tmp_path / "data" / "history.sqlite3"
    config.parent.mkdir(); data.parent.mkdir()
    config.write_text("config", encoding="utf-8"); data.write_text("data", encoding="utf-8")
    updater = engine(tmp_path, health=Health(False))
    updater.prepare(manifest); updater.consent_download(); updater.download(manifest)
    updater.consent_install(manifest)
    assert updater.install(manifest) is UpdateState.ROLLED_BACK
    assert updater.target_path.read_bytes() == b"old-version"
    assert config.read_text() == "config" and data.read_text() == "data"


class CrashAfterReplace:
    def __init__(self):
        self.atomic = AtomicFileUpdateInstaller()

    def install(self, plan):
        self.atomic.install(plan)
        raise KeyboardInterrupt("simulated process crash")

    def rollback(self, plan):
        self.atomic.rollback(plan)


class CrashDuringDownload:
    def download(self, artifact, destination, *, timeout_seconds):
        destination.write_bytes(b"part")
        raise KeyboardInterrupt("simulated download crash")


class IrrecoverableInstaller:
    def install(self, plan):
        raise RuntimeError("install failed before backup")

    def rollback(self, plan):
        raise RuntimeError("backup unavailable")


def test_crash_resume_rolls_back_an_interrupted_install(tmp_path):
    crash_case = load_case("adversarial-cases.json")["crash_rollback"]
    manifest = signed_manifest()
    updater = engine(tmp_path, installer=CrashAfterReplace())
    updater.prepare(manifest); updater.consent_download(); updater.download(manifest)
    updater.consent_install(manifest)
    with pytest.raises(KeyboardInterrupt):
        updater.install(manifest)
    assert updater.load_journal().state is UpdateState.INSTALLING
    assert updater.target_path.read_bytes() == PAYLOAD
    assert updater.resume(manifest).value == crash_case["expected"]
    assert updater.target_path.read_bytes() == b"old-version"


def test_crash_resume_discards_partial_download_and_requires_new_download(tmp_path):
    manifest = signed_manifest()
    updater = engine(tmp_path, downloader=CrashDuringDownload())
    updater.prepare(manifest); updater.consent_download()
    with pytest.raises(KeyboardInterrupt):
        updater.download(manifest)
    assert updater.load_journal().state is UpdateState.DOWNLOADING
    assert updater.resume(manifest) is UpdateState.DOWNLOAD_CONSENTED
    assert not (updater.cache_dir / updater.load_journal().partial_name).exists()


def test_failed_install_without_rollback_copy_enters_manual_recovery(tmp_path):
    manifest = signed_manifest()
    updater = engine(tmp_path, installer=IrrecoverableInstaller())
    updater.prepare(manifest); updater.consent_download(); updater.download(manifest)
    updater.consent_install(manifest)
    assert updater.install(manifest) is UpdateState.MANUAL_RECOVERY
    with pytest.raises(Exception, match="already in progress or requires recovery"):
        updater.prepare(manifest)


class FakeResponse:
    def __init__(self, payload, *, final_url, content_length=None):
        self.payload = payload; self.offset = 0; self.final_url = final_url; self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def geturl(self): return self.final_url
    def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]; self.offset += len(chunk); return chunk


class FakeOpener:
    def __init__(self, response): self.response = response
    def open(self, request, timeout): return self.response


def descriptor(manifest=None):
    return (manifest or signed_manifest()).artifacts[0]


def downloader(tmp_path, response, *, free=10**9):
    download = HttpsArtifactDownloader(
        allowed_hosts={"downloads.example.com"}, reserve_bytes=0,
        disk_usage=lambda path: shutil._ntuple_diskusage(10**9, 0, free),
    )
    download._opener = FakeOpener(response)
    return download


def test_downloader_rejects_redirect_oversize_hash_and_low_disk(tmp_path):
    cases = load_case("adversarial-cases.json")
    artifact = descriptor()
    evil = downloader(
        tmp_path,
        FakeResponse(PAYLOAD, final_url=f"https://{cases['redirect']['final_host']}/a"),
    )
    with pytest.raises(URLValidationError):
        evil.download(artifact, tmp_path / "redirect", timeout_seconds=1)

    oversized = downloader(tmp_path, FakeResponse(PAYLOAD + b"!", final_url=artifact.url))
    with pytest.raises(ArtifactDownloadError, match="exceeds"):
        oversized.download(artifact, tmp_path / "oversized", timeout_seconds=1)

    announced = downloader(
        tmp_path,
        FakeResponse(
            PAYLOAD,
            final_url=artifact.url,
            content_length=str(cases["oversize"]["announced_size"]),
        ),
    )
    with pytest.raises(ArtifactDownloadError, match="Content-Length"):
        announced.download(artifact, tmp_path / "announced", timeout_seconds=1)

    bad_hash = downloader(
        tmp_path,
        FakeResponse(cases["hash"]["body"].encode(), final_url=artifact.url),
    )
    with pytest.raises(ArtifactDownloadError, match="SHA-256"):
        bad_hash.download(artifact, tmp_path / "hash", timeout_seconds=1)

    low_disk = downloader(
        tmp_path,
        FakeResponse(PAYLOAD, final_url=artifact.url),
        free=cases["disk"]["free_bytes"],
    )
    with pytest.raises(InsufficientDiskSpaceError):
        low_disk.download(artifact, tmp_path / "disk", timeout_seconds=1)

    existing = tmp_path / "existing"
    existing.write_bytes(b"do-not-delete")
    safe = downloader(tmp_path, FakeResponse(PAYLOAD, final_url=artifact.url))
    with pytest.raises(ArtifactDownloadError):
        safe.download(artifact, existing, timeout_seconds=1)
    assert existing.read_bytes() == b"do-not-delete"


class RecordingHelper:
    def __init__(self): self.plans = []
    def execute(self, plan): self.plans.append(plan); return True


def test_windows_replacement_is_an_injected_helper_plan(tmp_path):
    helper = RecordingHelper()
    updater = engine(tmp_path, installer=WindowsHelperUpdateInstaller(helper))
    manifest = signed_manifest()
    updater.prepare(manifest); updater.consent_download(); updater.download(manifest)
    updater.consent_install(manifest)
    assert updater.install(manifest) is UpdateState.COMMITTED
    assert [plan.operation for plan in helper.plans] == ["install"]
    assert updater.target_path.read_bytes() == b"old-version"
