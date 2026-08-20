import base64
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quota_monitor.platform_adapters.update_install import (
    AtomicFileUpdateInstaller,
    FailClosedUpdateInstaller,
    UpdateInstallError,
    UpdateInstallPlan,
)
from quota_monitor.platform_paths import AppPaths
from quota_monitor.semver import SemVer
from quota_monitor.update_crypto import Ed25519ManifestSigner, build_ed25519_keyring
from quota_monitor.update_engine import UpdateEngine, UpdateState
from quota_monitor.update_manifest import ManifestParser, ManifestPolicy, UpdateCheckResult, UpdateStatus
from quota_monitor.update_runtime import (
    ExecutableSmokeHealthChecker,
    UpdateRuntimeError,
    UpdateRuntimeService,
    build_product_update_runtime,
    load_product_update_trust,
)


NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)
PAYLOAD = b"release"


class StaticSource:
    def __init__(self, manifest):
        self.manifest = manifest

    def check(self):
        return UpdateCheckResult(UpdateStatus.AVAILABLE, self.manifest)


class Downloader:
    def download(self, artifact, destination, *, timeout_seconds):
        destination.write_bytes(PAYLOAD)


class Health:
    def __init__(self, healthy=True):
        self.healthy = healthy

    def check(self, *, expected_version, timeout_seconds):
        return self.healthy


class CrashDownloader:
    def download(self, artifact, destination, *, timeout_seconds):
        destination.write_bytes(b"part")
        raise KeyboardInterrupt("simulated process crash")


def manifest_parser_and_value():
    signer = Ed25519ManifestSigner.from_private_bytes(os.urandom(32))
    document = {
        "schema_version": 1,
        "channel": "stable",
        "version": "0.3.0",
        "published_at": "2026-08-01T00:00:00Z",
        "expires_at": "2099-12-01T00:00:00Z",
        "minimum_updater_version": "0.2.0",
        "critical": False,
        "artifacts": [
            {
                "os": "windows",
                "arch": "x86_64",
                "format": "portable",
                "url": "https://downloads.example.com/releases/tracker.exe",
                "size": len(PAYLOAD),
                "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
            }
        ],
        "release_notes_url": "https://releases.example.com/v0.3.0",
    }
    parser = ManifestParser(
        policy=ManifestPolicy(
            allowed_artifact_hosts={"downloads.example.com"},
            allowed_release_notes_hosts={"releases.example.com"},
        ),
        verifier=build_ed25519_keyring({"fixture-key": signer.public_key_bytes}),
        now=lambda: NOW,
    )
    return parser, parser.parse(signer.sign(document, key_id="fixture-key"))


def service(tmp_path, *, downloader=None, health=None, task_runner=lambda action: action()):
    parser, manifest = manifest_parser_and_value()
    target = tmp_path / "install" / "tracker.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    engine = UpdateEngine(
        current_version=SemVer.parse("0.2.1"),
        channel="stable",
        platform_os="windows",
        platform_arch="x86_64",
        artifact_format="portable",
        cache_dir=tmp_path / "cache",
        target_path=target,
        downloader=downloader or Downloader(),
        installer=AtomicFileUpdateInstaller(),
        health_checker=health or Health(),
        now=lambda: NOW,
    )
    runtime = UpdateRuntimeService(
        source=StaticSource(manifest),
        parser=parser,
        engine=engine,
        task_runner=task_runner,
    )
    return runtime, manifest, target


def test_product_runtime_requires_version_bound_download_and_install_consent(tmp_path):
    runtime, _manifest, target = service(tmp_path)

    assert runtime.status()["state"] == "idle"
    assert runtime.check().status is UpdateStatus.AVAILABLE
    available = runtime.status()
    assert available["state"] == "available"
    assert available["release_notes_url"] == "https://releases.example.com/v0.3.0"
    assert "sha256" not in json.dumps(available)
    assert "downloads.example.com" not in json.dumps(available)

    with pytest.raises(UpdateRuntimeError) as changed:
        runtime.consent_download(version="0.3.1", confirm=True)
    assert changed.value.code == "update_version_changed"
    with pytest.raises(UpdateRuntimeError) as missing:
        runtime.consent_download(version="0.3.0", confirm=False)
    assert missing.value.code == "update_consent_required"

    assert runtime.consent_download(version="0.3.0", confirm=True)["state"] == "download-consented"
    assert runtime.start_download()["state"] == "staged"
    assert runtime.status()["progress"]["percent"] == 100
    assert runtime.consent_install(version="0.3.0", confirm=True)["state"] == "install-consented"
    assert runtime.start_install()["state"] == "committed"
    assert target.read_bytes() == PAYLOAD


def test_read_only_update_check_does_not_create_a_journal(tmp_path):
    runtime, _manifest, _target = service(tmp_path)

    result = runtime.check(prepare=False)

    assert result.status is UpdateStatus.AVAILABLE
    assert runtime.status()["state"] == "idle"
    assert not runtime.engine.journal_path.exists()


def test_failed_health_check_is_visible_as_safe_rollback_state(tmp_path):
    runtime, _manifest, target = service(tmp_path, health=Health(False))
    runtime.check()
    runtime.consent_download(version="0.3.0", confirm=True)
    runtime.start_download()
    runtime.consent_install(version="0.3.0", confirm=True)

    status = runtime.start_install()

    assert status["state"] == "rolled-back"
    assert status["last_error_code"] == "update_rolled_back"
    assert target.read_bytes() == b"old-version"
    persisted = json.loads(runtime.runtime_status_path.read_text(encoding="utf-8"))
    assert persisted["last_error_code"] == "update_rolled_back"


def test_wrong_installed_version_fails_health_check_and_rolls_back(tmp_path, monkeypatch):
    target = tmp_path / "install" / "tracker.exe"
    checker = ExecutableSmokeHealthChecker(target)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        assert args[1] == "--version"
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"{target.name} 9.9.9\n".encode(),
        )

    monkeypatch.setattr("quota_monitor.update_runtime.subprocess.run", fake_run)
    runtime, _manifest, installed = service(tmp_path, health=checker)
    runtime.check()
    runtime.consent_download(version="0.3.0", confirm=True)
    runtime.start_download()
    runtime.consent_install(version="0.3.0", confirm=True)

    status = runtime.start_install()

    assert status["state"] == "rolled-back"
    assert installed.read_bytes() == b"old-version"
    assert calls == [[str(installed.resolve()), "--version"]]


def test_crash_journal_is_observable_and_resume_discards_partial_file(tmp_path):
    runtime, manifest, _target = service(tmp_path, downloader=CrashDownloader())
    runtime.check()
    runtime.consent_download(version="0.3.0", confirm=True)
    with pytest.raises(KeyboardInterrupt):
        runtime.engine.download(manifest)

    restarted = UpdateRuntimeService(
        source=runtime.source,
        parser=runtime.parser,
        engine=runtime.engine,
        task_runner=lambda action: action(),
    )
    interrupted = restarted.status()
    assert interrupted["state"] == "downloading"
    assert interrupted["recovery_required"] is True

    resumed = restarted.resume()
    assert resumed["state"] == "download-consented"
    assert resumed["recovery_required"] is False


def test_tampered_cached_manifest_fails_closed_without_exposing_contents(tmp_path):
    runtime, _manifest, _target = service(tmp_path)
    runtime.check()
    runtime.manifest_path.write_text('{"url":"https://evil.invalid/secret"}', encoding="utf-8")

    status = runtime.status()

    assert status["release_notes_url"] is None
    assert status["last_error_code"] == "authenticated_manifest_unavailable"
    runtime.consent_download(version="0.3.0", confirm=True)
    # Consent advances only the journal. The execution boundary authenticates
    # the cached manifest again and refuses the artifact operation.
    with pytest.raises(UpdateRuntimeError) as failure:
        runtime.start_download()
    assert failure.value.code == "authenticated_manifest_unavailable"


def write_trust(path: Path, public_key: bytes, **changes):
    document = {
        "schema_version": 1,
        "manifest_urls": {
            "stable": "https://updates.example.com/stable.json",
            "beta": "https://updates.example.com/beta.json",
        },
        "manifest_hosts": ["updates.example.com"],
        "artifact_hosts": ["downloads.example.com"],
        "release_notes_hosts": ["releases.example.com"],
        "public_keys": {"release-key": base64.b64encode(public_key).decode("ascii")},
    }
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_bundled_trust_rejects_non_https_and_product_builder_uses_injected_target(tmp_path):
    signer = Ed25519ManifestSigner.from_private_bytes(os.urandom(32))
    trust_path = tmp_path / "trust.json"
    write_trust(trust_path, signer.public_key_bytes)
    trust = load_product_update_trust(trust_path)
    assert trust.manifest_hosts == frozenset({"updates.example.com"})
    assert set(trust.manifest_urls) == {"beta", "stable"}

    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )
    target = tmp_path / "tracker.exe"
    target.write_bytes(b"old")
    windows_runtime = build_product_update_runtime(
        paths=paths,
        channel="stable",
        current_version="0.2.1",
        trust_path=trust_path,
        target_path=target,
        platform_os="windows",
        platform_arch="x86_64",
        artifact_format="portable",
    )
    assert windows_runtime is not None
    assert windows_runtime.source.manifest_url == "https://updates.example.com/stable.json"
    assert windows_runtime.installation_available is False
    assert isinstance(windows_runtime.engine.installer, FailClosedUpdateInstaller)
    assert windows_runtime.status()["can_resume"] is False

    linux_runtime = build_product_update_runtime(
        paths=paths,
        channel="stable",
        current_version="0.2.1",
        trust_path=trust_path,
        target_path=target,
        platform_os="linux",
        platform_arch="x86_64",
        artifact_format="appimage",
    )
    assert linux_runtime is not None
    assert linux_runtime.installation_available is True
    assert isinstance(linux_runtime.engine.installer, AtomicFileUpdateInstaller)

    macos_runtime = build_product_update_runtime(
        paths=paths,
        channel="stable",
        current_version="0.2.1",
        trust_path=trust_path,
        target_path=target,
        platform_os="macos",
        platform_arch="x86_64",
        artifact_format="app-bundle",
    )
    assert macos_runtime is not None
    assert macos_runtime.installation_available is False
    assert isinstance(macos_runtime.engine.installer, FailClosedUpdateInstaller)

    beta_runtime = build_product_update_runtime(
        paths=paths,
        channel="beta",
        current_version="1.0.0-rc.1",
        trust_path=trust_path,
        target_path=target,
        platform_os="linux",
        platform_arch="x86_64",
        artifact_format="appimage",
    )
    assert beta_runtime is not None
    assert beta_runtime.source.manifest_url == "https://updates.example.com/beta.json"

    write_trust(
        trust_path,
        signer.public_key_bytes,
        manifest_urls={
            "stable": "http://updates.example.com/stable.json",
            "beta": "https://updates.example.com/beta.json",
        },
    )
    with pytest.raises(ValueError, match="bundled update trust is invalid"):
        load_product_update_trust(trust_path)


def test_bundled_trust_keyring_is_bounded_and_rejects_duplicate_or_invalid_ids(tmp_path):
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    trust_path = tmp_path / "trust.json"
    common = {
        "schema_version": 1,
        "manifest_urls": {
            "stable": "https://updates.example.com/stable.json",
            "beta": "https://updates.example.com/beta.json",
        },
        "manifest_hosts": ["updates.example.com"],
        "artifact_hosts": ["downloads.example.com"],
        "release_notes_hosts": ["releases.example.com"],
    }
    for keys in (
        {},
        {f"key-{index}": key for index in range(5)},
        {"bad key id": key},
    ):
        trust_path.write_text(json.dumps({**common, "public_keys": keys}), encoding="utf-8")
        with pytest.raises(ValueError, match="bundled update trust is invalid"):
            load_product_update_trust(trust_path)
    trust_path.write_text(
        json.dumps({**common, "public_keys": {"old": key}}).replace(
            '"old":', '"old": "' + key + '", "old":'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bundled update trust is invalid"):
        load_product_update_trust(trust_path)


def test_fail_closed_installer_never_mutates_an_unsupported_running_target(tmp_path):
    target = tmp_path / "tracker.exe"
    staged = tmp_path / "tracker.staged"
    target.write_bytes(b"running")
    staged.write_bytes(PAYLOAD)
    plan = UpdateInstallPlan(
        staged_path=staged,
        target_path=target,
        backup_path=tmp_path / "tracker.previous",
        journal_path=tmp_path / "journal.json",
        expected_size=len(PAYLOAD),
        expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    )
    installer = FailClosedUpdateInstaller()

    with pytest.raises(UpdateInstallError, match="unavailable"):
        installer.install(plan)
    with pytest.raises(UpdateInstallError, match="unavailable"):
        installer.rollback(plan)

    assert target.read_bytes() == b"running"
    assert not plan.backup_path.exists()


def test_unavailable_installer_blocks_interrupted_install_recovery(tmp_path):
    runtime, _manifest, target = service(tmp_path)
    runtime.check()
    runtime.consent_download(version="0.3.0", confirm=True)
    runtime.start_download()
    runtime.consent_install(version="0.3.0", confirm=True)
    document = json.loads(runtime.engine.journal_path.read_text(encoding="utf-8"))
    document["state"] = "installing"
    runtime.engine.journal_path.write_text(json.dumps(document), encoding="utf-8")
    unavailable = UpdateRuntimeService(
        source=runtime.source,
        parser=runtime.parser,
        engine=runtime.engine,
        installation_available=False,
        task_runner=lambda action: action(),
    )

    status = unavailable.status()
    assert status["can_resume"] is False
    with pytest.raises(UpdateRuntimeError) as failure:
        unavailable.resume()
    assert failure.value.code == "installer_helper_unavailable"
    assert target.read_bytes() == b"old-version"
