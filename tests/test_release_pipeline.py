from __future__ import annotations

import base64
import hashlib
import json
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from quota_monitor import __version__
from quota_monitor import update_cli
from quota_monitor.semver import SemVer
from quota_monitor.update_crypto import Ed25519ManifestSigner
from scripts import (
    build_release,
    fetch_pinned_tool,
    generate_sbom,
    generate_update_manifest,
    generate_update_trust,
    release_entry,
    release_metadata,
    sign_artifact_manifest,
    validate_channel_promotion,
    verify_packaged_artifact,
    verify_release_reuse,
)


ROOT = Path(__file__).resolve().parents[1]


def _signed_channel(version: str) -> str:
    return "beta" if SemVer.parse(version).is_prerelease else "stable"


def _write_complete_artifact_set(
    directory: Path, version: str, *, channel: str = "candidate"
) -> list[Path]:
    names = [
        f"OpenAI-Free-Credit-Tracker-{version}-windows-x86_64-portable.exe",
        f"OpenAI-Free-Credit-Tracker-{version}-windows-x86_64-setup.exe",
        f"OpenAI-Free-Credit-Tracker-{version}-windows-x86_64.cdx.json",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-x86_64.app.zip",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-x86_64.dmg",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-x86_64.cdx.json",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-arm64.app.zip",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-arm64.dmg",
        f"OpenAI-Free-Credit-Tracker-{version}-macos-arm64.cdx.json",
        f"OpenAI-Free-Credit-Tracker-{version}-linux-x86_64.tar.gz",
        f"OpenAI-Free-Credit-Tracker-{version}-linux-x86_64.AppImage",
        f"OpenAI-Free-Credit-Tracker-{version}-linux-x86_64.cdx.json",
    ]
    if channel in {"beta", "stable"}:
        names.extend(
            [
                f"OpenAI-Free-Credit-Tracker-{version}-linux-x86_64.tar.gz.asc",
                f"OpenAI-Free-Credit-Tracker-{version}-linux-x86_64.AppImage.asc",
                f"OpenAI-Free-Credit-Tracker-{version}-update-manifest.json",
                generate_update_trust.channel_manifest_name(channel),
            ]
        )
    else:
        names.append(
            f"OpenAI-Free-Credit-Tracker-{version}-update-manifest.unsigned.json"
        )
    names.extend(
        [
            f"OpenAI-Free-Credit-Tracker-{version}-dependency-audit.json",
            f"OpenAI-Free-Credit-Tracker-{version}-license-inventory.json",
            f"OpenAI-Free-Credit-Tracker-{version}-quality-evidence.json",
        ]
    )
    paths = [directory / name for name in names]
    for path in paths:
        path.write_bytes(f"release payload: {path.name}".encode())
    if channel in {"beta", "stable"}:
        versioned = directory / f"OpenAI-Free-Credit-Tracker-{version}-update-manifest.json"
        (directory / generate_update_trust.channel_manifest_name(channel)).write_bytes(
            versioned.read_bytes()
        )
    (directory / f"OpenAI-Free-Credit-Tracker-{version}-dependency-audit.json").write_text(
        json.dumps(
            {
                "dependencies": [
                    {"name": "cryptography", "version": "1", "vulns": []},
                    {
                        "name": "openai-free-credit-tracker",
                        "skip_reason": "distribution marked as editable",
                    },
                ],
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )
    (directory / f"OpenAI-Free-Credit-Tracker-{version}-license-inventory.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "packages": [
                    {"name": "cryptography", "version": "1", "licenses": ["Apache-2.0"]}
                ],
                "summary": {
                    "package_count": 1,
                    "unknown_license_count": 0,
                    "unknown_license_packages": [],
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / f"OpenAI-Free-Credit-Tracker-{version}-quality-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": True,
                "reports": [
                    {
                        "kind": "synthetic_performance",
                        "passed": True,
                        "scenario": {"days": 365, "projects": 100, "records": 36500},
                    },
                    {
                        "kind": "accelerated_simulated_soak",
                        "passed": True,
                        "scenario": {"simulated_hours": 72},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    scan_targets = sorted(path for path in directory.iterdir() if path.is_file())
    malware_path = directory / f"OpenAI-Free-Credit-Tracker-{version}-malware-scan.json"
    malware_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scanner": "clamscan",
                "scanner_version": "ClamAV 1.4.3/27844/Tue Aug 19 00:00:00 2026",
                "passed": True,
                "artifacts": [
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "sha256": release_metadata.sha256_file(path),
                        "status": "clean",
                    }
                    for path in scan_targets
                ],
            }
        ),
        encoding="utf-8",
    )
    paths.append(malware_path)
    return paths


def test_platform_sbom_records_exact_native_library_hash(tmp_path, monkeypatch):
    native = tmp_path / "libssl.3.dylib"
    native.write_bytes(b"audited native library")
    output = tmp_path / "platform.cdx.json"

    def fake_cyclonedx(command, **_kwargs):
        generated = Path(command[command.index("--output-file") + 1])
        generated.write_text(
            json.dumps(
                {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []}
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(generate_sbom.subprocess, "run", fake_cyclonedx)
    generate_sbom.generate_sbom(
        output,
        native_components=[("openssl", "3.5.4", native)],
    )

    component = json.loads(output.read_text(encoding="utf-8"))["components"][0]
    assert component["name"] == "openssl:libssl.3.dylib"
    assert component["version"] == "3.5.4"
    assert component["hashes"] == [
        {"alg": "SHA-256", "content": hashlib.sha256(native.read_bytes()).hexdigest()}
    ]


def test_packaged_self_test_covers_locales_database_exports_and_clean_shutdown(capsys):
    release_entry.packaged_self_test()
    assert f"{__version__} packaged self-test passed" in capsys.readouterr().out


def test_packaged_import_smoke_is_explicitly_trust_unavailable_for_source_tree(capsys):
    assert release_entry.packaged_import_smoke() is False
    assert "update trust absent" in capsys.readouterr().out


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "linux"])
def test_packaged_desktop_dependency_check_only_imports_required_modules(platform_name):
    imported = []

    def importer(name):
        imported.append(name)
        return object()

    required = release_entry.validate_desktop_runtime_imports(
        platform_name, importer=importer
    )
    assert tuple(imported) == required
    assert "desktop_notifier" in required
    expected_notifier_backend = {
        "win32": "desktop_notifier.backends.winrt",
        "darwin": "desktop_notifier.backends.macos",
        "linux": "desktop_notifier.backends.dbus",
    }[platform_name]
    assert expected_notifier_backend in required
    assert "pystray" in required
    assert "PIL.Image" in required
    if platform_name == "darwin":
        assert "rubicon.objc.eventloop" in required


def test_release_metadata_round_trip_and_tamper_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1234567890")
    signed_channel = _signed_channel(__version__)
    artifacts = _write_complete_artifact_set(tmp_path, __version__, channel=signed_channel)
    commit = "a" * 40

    manifest = release_metadata.generate(
        tmp_path,
        version=__version__,
        source_commit=commit,
        channel=signed_channel,
        repository="example/project",
        run_id="123",
        windows_identity="Windows Publisher",
        macos_identity="Developer ID Application: Example",
        linux_fingerprint="A" * 40,
        update_key_id="release-key-1",
    )
    assert manifest["generated_at"] == "2009-02-13T23:31:30Z"
    assert len(manifest["artifacts"]) == len(
        release_metadata.CORE_RELEASE_SLOTS
        | release_metadata.STABLE_SIGNING_SLOTS
        | release_metadata.QUALITY_EVIDENCE_SLOTS
    )
    assert manifest["signing"]["linux"]["fingerprint"] == "A" * 40
    assert manifest["provenance"]["run_id"] == "123"
    seed = b"\x17" * 32
    signer = Ed25519ManifestSigner.from_private_bytes(seed)
    monkeypatch.setenv("OAI_UPDATE_PRIVATE_KEY_B64", base64.b64encode(seed).decode())
    monkeypatch.setenv(
        "OAI_UPDATE_PUBLIC_KEYS_JSON",
        json.dumps(
            {"release-key-1": base64.b64encode(signer.public_key_bytes).decode()}
        ),
    )
    manifest_path = tmp_path / release_metadata.MANIFEST_NAME
    assert (
        sign_artifact_manifest.main(
            [
                "sign",
                "--input",
                str(manifest_path),
                "--output",
                str(manifest_path),
                "--key-id",
                "release-key-1",
            ]
        )
        == 0
    )
    assert sign_artifact_manifest.main(["verify", "--input", str(manifest_path)]) == 0
    release_metadata.verify(
        tmp_path,
        version=__version__,
        source_commit=commit,
        channel=signed_channel,
        tag=f"v{__version__}",
    )

    artifacts[0].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="(size|checksum) mismatch"):
        release_metadata.verify(
            tmp_path,
            version=__version__,
            source_commit=commit,
            channel=signed_channel,
            tag=f"v{__version__}",
        )


def test_release_metadata_rejects_short_commit_and_wrong_tag(tmp_path):
    _write_complete_artifact_set(tmp_path, __version__)
    with pytest.raises(ValueError, match="full lowercase"):
        release_metadata.generate(
            tmp_path, version=__version__, source_commit="abc123", channel="candidate"
        )
    release_metadata.generate(
        tmp_path, version=__version__, source_commit="b" * 40, channel="candidate"
    )
    with pytest.raises(ValueError, match="does not equal package version"):
        release_metadata.verify(
            tmp_path,
            version=__version__,
            source_commit="b" * 40,
            channel="candidate",
            tag="v9.9.9",
        )


def test_beta_and_stable_metadata_reject_the_wrong_semver_class(tmp_path):
    with pytest.raises(ValueError, match="beta release metadata requires"):
        release_metadata.generate(
            tmp_path,
            version="1.0.0",
            source_commit="a" * 40,
            channel="beta",
        )
    with pytest.raises(ValueError, match="stable release metadata requires"):
        release_metadata.generate(
            tmp_path,
            version="1.0.0-rc.1",
            source_commit="a" * 40,
            channel="stable",
        )


def test_release_metadata_rejects_incomplete_quality_and_malware_evidence(tmp_path):
    quality_root = tmp_path / "quality"
    quality_root.mkdir()
    _write_complete_artifact_set(quality_root, __version__)
    quality = quality_root / f"OpenAI-Free-Credit-Tracker-{__version__}-quality-evidence.json"
    document = json.loads(quality.read_text(encoding="utf-8"))
    document["reports"][0]["scenario"]["projects"] = 99
    quality.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="full performance/soak scenarios"):
        release_metadata.generate(
            quality_root,
            version=__version__,
            source_commit="c" * 40,
            channel="candidate",
        )

    malware_root = tmp_path / "malware"
    malware_root.mkdir()
    _write_complete_artifact_set(malware_root, __version__)
    malware = malware_root / f"OpenAI-Free-Credit-Tracker-{__version__}-malware-scan.json"
    document = json.loads(malware.read_text(encoding="utf-8"))
    document["artifacts"].pop()
    malware.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="complete immutable artifact set"):
        release_metadata.generate(
            malware_root,
            version=__version__,
            source_commit="c" * 40,
            channel="candidate",
        )


def test_update_manifest_draft_uses_final_distributable_hashes_and_release_urls(
    tmp_path, monkeypatch
):
    paths = _write_complete_artifact_set(tmp_path, __version__)
    (tmp_path / f"OpenAI-Free-Credit-Tracker-{__version__}-update-manifest.unsigned.json").unlink()
    output = tmp_path / "draft.json"
    document = generate_update_manifest.generate(
        tmp_path,
        output,
        version=__version__,
        source_epoch=1_786_224_000,
        repository="example/project",
        channel=_signed_channel(__version__),
    )
    assert document["minimum_updater_version"] == "1.0.0-rc.1"
    assert SemVer.parse(document["minimum_updater_version"]) < SemVer.parse("1.0.0-rc.2")
    assert len(document["artifacts"]) == 8
    assert all(
        item["url"].startswith(
            f"https://github.com/example/project/releases/download/v{__version__}/"
        )
        for item in document["artifacts"]
    )
    expected = {
        path.name: release_metadata.sha256_file(path)
        for path in paths
        if path.suffix in {".exe", ".zip", ".dmg", ".gz", ".AppImage"}
    }
    assert {Path(item["url"]).name: item["sha256"] for item in document["artifacts"]} == expected
    seed = b"\x23" * 32
    signer = Ed25519ManifestSigner.from_private_bytes(seed)
    monkeypatch.setenv("OAI_UPDATE_PRIVATE_KEY_B64", base64.b64encode(seed).decode())
    signed = tmp_path / "signed-update.json"
    assert (
        update_cli.main(
            [
                "sign",
                "--input",
                str(output),
                "--output",
                str(signed),
                "--key-id",
                "release-key-1",
                "--artifact-host",
                "github.com",
                "--release-notes-host",
                "github.com",
            ]
        )
        == 0
    )
    monkeypatch.delenv("OAI_UPDATE_PRIVATE_KEY_B64")
    monkeypatch.setenv(
        "OAI_UPDATE_PUBLIC_KEYS_JSON",
        json.dumps(
            {"release-key-1": base64.b64encode(signer.public_key_bytes).decode()}
        ),
    )
    assert (
        update_cli.main(
            [
                "verify",
                "--input",
                str(signed),
                "--artifact-host",
                "github.com",
                "--release-notes-host",
                "github.com",
            ]
        )
        == 0
    )


def test_update_manifest_channel_matches_version_class(tmp_path):
    with pytest.raises(ValueError, match="stable update manifests require"):
        generate_update_manifest.generate(
            tmp_path,
            tmp_path / "stable.json",
            version="1.0.0-rc.1",
            source_epoch=1,
            repository="example/project",
            channel="stable",
        )
    with pytest.raises(ValueError, match="beta update manifests require"):
        generate_update_manifest.generate(
            tmp_path,
            tmp_path / "beta.json",
            version="1.0.0",
            source_epoch=1,
            repository="example/project",
            channel="beta",
        )


def test_stable_update_trust_is_deterministic_public_only_and_fail_closed(
    tmp_path, monkeypatch
):
    signer = Ed25519ManifestSigner.from_private_bytes(bytes(range(32)))
    public_key = base64.b64encode(signer.public_key_bytes).decode("ascii")
    trust_path = tmp_path / "data" / "update-trust.json"
    document = generate_update_trust.generate(
        trust_path,
        key_id="release-key-2026",
        public_key_b64=public_key,
    )
    assert generate_update_trust.load_and_validate(trust_path) == document
    serialized = trust_path.read_text(encoding="utf-8")
    assert "private" not in serialized.lower()
    assert document["manifest_urls"] == {
        "beta": (
            "https://raw.githubusercontent.com/kejamestw/openai-free-credit-tracker/"
            "update-channels/beta.json"
        ),
        "stable": (
            "https://github.com/kejamestw/openai-free-credit-tracker/releases/latest/download/"
            "OpenAI-Free-Credit-Tracker-stable-update-manifest.json"
        ),
    }
    generate_update_trust.generate(
        trust_path,
        key_id="release-key-2026",
        public_key_b64=public_key,
    )
    with pytest.raises(ValueError, match="refusing to replace"):
        generate_update_trust.generate(
            trust_path,
            key_id="different-key",
            public_key_b64=public_key,
        )

    monkeypatch.setattr(build_release, "UPDATE_TRUST_PATH", trust_path)
    monkeypatch.setenv("UPDATE_SIGNING_KEY_ID", "release-key-2026")
    monkeypatch.setenv("UPDATE_SIGNING_PUBLIC_KEY_B64", public_key)
    build_release._validate_update_trust_policy("stable")
    build_release._validate_update_trust_policy("beta")
    trust_path.unlink()
    build_release._validate_update_trust_policy("candidate")
    with pytest.raises(RuntimeError, match="valid immutable update trust"):
        build_release._validate_update_trust_policy("beta")
    with pytest.raises(RuntimeError, match="protected update public key"):
        monkeypatch.delenv("UPDATE_SIGNING_KEY_ID")
        build_release._validate_update_trust_policy("stable")


def test_update_trust_supports_bounded_overlapping_rotation_keyring(tmp_path):
    active = base64.b64encode(bytes(range(32))).decode("ascii")
    next_key = base64.b64encode(bytes(range(1, 33))).decode("ascii")
    path = tmp_path / "trust.json"
    document = generate_update_trust.generate(
        path,
        key_id="active-2026",
        public_key_b64=active,
        public_keys_b64={"next-2027": next_key, "active-2026": active},
    )
    assert list(document["public_keys"]) == ["active-2026", "next-2027"]
    with pytest.raises(ValueError, match="between one and four"):
        generate_update_trust.build_document(
            key_id="active-2026",
            public_key_b64=active,
            repository=generate_update_trust.DEFAULT_REPOSITORY,
            public_keys_b64={f"key-{index}": active for index in range(5)},
        )
    with pytest.raises(ValueError, match="missing or mismatched"):
        generate_update_trust.build_document(
            key_id="active-2026",
            public_key_b64=active,
            repository=generate_update_trust.DEFAULT_REPOSITORY,
            public_keys_b64={"next-2027": next_key},
        )


def test_beta_channel_promotion_is_signed_prerelease_and_monotonic():
    def manifest(version: str, *, channel: str = "beta") -> dict:
        return {
            "schema_version": 1,
            "channel": channel,
            "version": version,
            "key_id": "release-key-2026",
            "signature": "signed-payload",
        }

    validate_channel_promotion.validate_promotion(
        manifest("1.0.0-rc.2"),
        manifest("1.0.0-rc.1"),
    )
    already_promoted = manifest("1.0.0-rc.2")
    validate_channel_promotion.validate_promotion(already_promoted, dict(already_promoted))
    with pytest.raises(ValueError, match="byte-identical"):
        validate_channel_promotion.validate_promotion(
            manifest("1.0.0-rc.2"),
            {**manifest("1.0.0-rc.2"), "signature": "different-signed-payload"},
        )
    with pytest.raises(ValueError, match="byte-identical"):
        validate_channel_promotion.validate_promotion(
            already_promoted,
            dict(already_promoted),
            exact_match=False,
        )
    with pytest.raises(ValueError, match="increase semantic version"):
        validate_channel_promotion.validate_promotion(
            manifest("1.0.0-rc.1"),
            manifest("1.0.0-rc.2"),
        )
    with pytest.raises(ValueError, match="schema-v1 beta"):
        validate_channel_promotion.validate_promotion(manifest("1.0.0", channel="stable"))


def test_existing_release_reuse_requires_the_exact_candidate_bytes(tmp_path):
    candidate = tmp_path / "candidate"
    published = tmp_path / "published"
    candidate.mkdir()
    published.mkdir()
    (candidate / "artifact.exe").write_bytes(b"signed bytes")
    (published / "artifact.exe").write_bytes(b"signed bytes")
    verify_release_reuse.verify_reuse(candidate, published)
    (candidate / "second.dmg").write_bytes(b"second")
    verify_release_reuse.verify_reuse(
        candidate, published, allow_published_subset=True
    )
    with pytest.raises(ValueError, match="asset names differ"):
        verify_release_reuse.verify_reuse(candidate, published)
    (candidate / "second.dmg").unlink()
    (published / "artifact.exe").write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="asset bytes differ"):
        verify_release_reuse.verify_reuse(candidate, published)
    (published / "artifact.exe").write_bytes(b"signed bytes")
    (published / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="asset names differ"):
        verify_release_reuse.verify_reuse(candidate, published)


def test_pinned_linux_toolchain_has_immutable_identity_size_and_sha():
    config_path = ROOT / "packaging" / "linux" / "toolchain.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(document["tools"]) == {"appimagetool-x86_64", "runtime-x86_64"}
    for name, tool in document["tools"].items():
        loaded = fetch_pinned_tool.load_tool(config_path, name)
        assert loaded == tool
        assert re.fullmatch(r"[0-9a-f]{40}", tool["repository_commit"])
        assert re.fullmatch(r"[0-9a-f]{64}", tool["sha256"])
        assert tool["url"].endswith(f"/assets/{tool['asset_id']}")
        assert tool["size"] > 100_000


def test_build_contract_is_native_and_bundles_all_runtime_resources(monkeypatch):
    monkeypatch.setattr(build_release, "normalized_host", lambda: ("windows", "x86_64"))
    build_release.require_native("windows", "x86_64")
    with pytest.raises(RuntimeError, match="native build requires"):
        build_release.require_native("linux", "x86_64")

    source = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    for resource in ('("web", "web")', '("data", "data")', '("locales", "locales")'):
        assert resource in source
    assert "--packaged-self-test" in (
        ROOT / "scripts" / "verify_packaged_artifact.py"
    ).read_text(encoding="utf-8")
    assert "--with-installer" in source
    assert "--runtime-file" in source
    assert '("desktop-notifier", "pystray", "Pillow")' in source
    for module in (
        '"desktop_notifier"',
        '"pystray"',
        '"PIL.Image"',
        '"pystray._win32"',
        '"pystray._darwin"',
        '"rubicon.objc.eventloop"',
        '"pystray._xorg"',
    ):
        assert module in source


@pytest.mark.parametrize(
    ("target_os", "target_arch", "backend"),
    [
        ("windows", "x86_64", "pystray._win32"),
        ("macos", "arm64", "rubicon.objc.eventloop"),
        ("linux", "x86_64", "pystray._xorg"),
    ],
)
def test_pyinstaller_command_collects_desktop_dependencies(
    tmp_path, monkeypatch, target_os, target_arch, backend
):
    commands = []
    monkeypatch.setattr(
        build_release,
        "run",
        lambda command, *, env=None: commands.append((command, env)),
    )

    build_release._pyinstaller(target_os, target_arch, tmp_path, __version__)

    command = commands[0][0]
    option_pairs = set(zip(command, command[1:]))
    for distribution in ("desktop-notifier", "pystray", "Pillow"):
        assert ("--copy-metadata", distribution) in option_pairs
    for package in ("desktop_notifier", "pystray", "PIL"):
        assert ("--collect-data", package) in option_pairs
    assert ("--hidden-import", backend) in option_pairs


def test_native_metadata_and_stable_macos_credentials_are_fail_closed(tmp_path, monkeypatch):
    version_file = build_release._windows_version_file(tmp_path, __version__)
    assert f"StringStruct('ProductVersion', '{__version__}')" in version_file.read_text(
        encoding="utf-8"
    )

    app = tmp_path / "Tracker.app" / "Contents"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleName": "Tracker"}, handle)
    build_release._set_macos_bundle_metadata(app.parent, __version__)
    with (app / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    native_versions = build_release._native_versions(__version__)
    assert info["CFBundleShortVersionString"] == native_versions[1]
    assert info["CFBundleVersion"] == native_versions[2]
    assert info["CFBundleIdentifier"] == "tw.kejames.openai-free-credit-tracker"
    assert build_release._native_versions("1.0.0-rc.1") == (
        "1.0.0.0",
        "1.0.0",
        "1.0.0fc1",
    )
    installer = (ROOT / "installer" / "windows" / "OpenAI-Free-Credit-Tracker.iss").read_text(
        encoding="utf-8"
    )
    assert "VersionInfoVersion={#NumericVersion}" in installer

    for name in (
        "MACOS_SIGNING_IDENTITY",
        "APPLE_API_KEY_PATH",
        "APPLE_API_KEY_ID",
        "APPLE_API_ISSUER",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_release._mac_credentials("candidate") is None
    with pytest.raises(RuntimeError, match="signed macOS candidates require"):
        build_release._mac_credentials("stable")
    monkeypatch.setenv("MACOS_SIGNING_IDENTITY", "Developer ID Application: Example")
    assert build_release._mac_credentials("candidate") is None
    with pytest.raises(RuntimeError, match="credentials are incomplete"):
        build_release._mac_credentials("stable")

    for name in (
        "SIGNTOOL_EXE",
        "WINDOWS_SIGNING_CERT_SHA1",
        "WINDOWS_SIGNING_IDENTITY",
        "WINDOWS_TIMESTAMP_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_release._windows_signing("candidate") is None
    with pytest.raises(RuntimeError, match="require Authenticode"):
        build_release._windows_signing("stable")
    monkeypatch.setenv("WINDOWS_SIGNING_IDENTITY", "Publisher")
    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        build_release._windows_signing("stable")


def test_macos_bundle_is_resigned_after_native_metadata_changes(tmp_path, monkeypatch):
    bundle = tmp_path / "Tracker.app"
    commands = []
    monkeypatch.setattr(build_release, "run", lambda command, **_kwargs: commands.append(command))

    build_release._sign_macos_bundle(bundle, None)
    assert commands[0] == [
        "codesign",
        "--force",
        "--deep",
        "--sign",
        "-",
        str(bundle),
    ]
    assert commands[1][:4] == ["codesign", "--verify", "--deep", "--strict"]

    commands.clear()
    build_release._sign_macos_bundle(bundle, "Developer ID Application: Example")
    assert "runtime" in commands[0]
    assert "--timestamp" in commands[0]
    assert "Developer ID Application: Example" in commands[0]
    assert commands[1][:4] == ["codesign", "--verify", "--deep", "--strict"]


def test_intel_macos_bundle_uses_cryptography_openssl_abi(tmp_path, monkeypatch):
    bundle = tmp_path / "Tracker.app"
    frameworks = bundle / "Contents" / "Frameworks"
    frameworks.mkdir(parents=True)
    source_ssl = tmp_path / "source" / "libssl.3.dylib"
    source_crypto = tmp_path / "source" / "libcrypto.3.dylib"
    source_ssl.parent.mkdir()
    source_ssl.write_bytes(b"new ssl abi")
    source_crypto.write_bytes(b"new crypto abi")
    (frameworks / source_ssl.name).write_bytes(b"old ssl abi")
    (frameworks / source_crypto.name).write_bytes(b"old crypto abi")
    commands = []
    monkeypatch.setattr(build_release, "run", lambda command, **_kwargs: commands.append(command))
    monkeypatch.setattr(
        build_release,
        "_macos_dependencies",
        lambda binary: (
            [Path("@loader_path/libcrypto.3.dylib")]
            if binary == frameworks / "libssl.3.dylib"
            else []
        ),
    )

    build_release._bundle_intel_macos_openssl(
        bundle,
        libraries={
            source_ssl.name: source_ssl,
            source_crypto.name: source_crypto,
        },
    )

    assert (frameworks / source_ssl.name).read_bytes() == b"new ssl abi"
    assert (frameworks / source_crypto.name).read_bytes() == b"new crypto abi"
    assert any(command[1:3] == ["-id", "@rpath/libssl.3.dylib"] for command in commands)
    assert any(command[1:3] == ["-id", "@rpath/libcrypto.3.dylib"] for command in commands)
    assert any(
        command[1:4]
        == ["-change", source_crypto.as_posix(), "@loader_path/libcrypto.3.dylib"]
        for command in commands
    )
    assert sum(command[:3] == ["lipo", "-verify_arch", "x86_64"] for command in commands) == 2


def test_intel_macos_openssl_discovery_follows_libssl_dependency(tmp_path, monkeypatch):
    binding = tmp_path / "_rust.abi3.so"
    ssl = tmp_path / "homebrew" / "libssl.3.dylib"
    crypto = tmp_path / "homebrew" / "libcrypto.3.dylib"
    binding.write_bytes(b"binding")
    ssl.parent.mkdir()
    ssl.write_bytes(b"ssl")
    crypto.write_bytes(b"crypto")

    class Spec:
        origin = str(binding)

    monkeypatch.setattr(build_release.importlib.util, "find_spec", lambda _name: Spec())
    monkeypatch.setattr(
        build_release,
        "_macos_dependencies",
        lambda binary: [ssl] if binary == binding.resolve() else [crypto],
    )

    assert build_release._linked_cryptography_openssl() == {
        "libssl.3.dylib": ssl,
        "libcrypto.3.dylib": crypto,
    }


@pytest.mark.parametrize("token", ["@loader_path/libssl.3.dylib", "@rpath/libssl.3.dylib"])
def test_macos_dependency_tokens_resolve_to_colocated_library(tmp_path, monkeypatch, token):
    binary = tmp_path / "_rust.abi3.so"
    library = tmp_path / "libssl.3.dylib"
    binary.write_bytes(b"binding")
    library.write_bytes(b"ssl")
    monkeypatch.setattr(build_release, "_macos_rpaths", lambda _binary: ["@loader_path"])

    assert build_release._resolve_macos_dependency(binary, Path(token)) == library


def test_packaged_verifier_reports_bounded_native_loader_diagnostics(tmp_path):
    def failed_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["artifact"],
            returncode=126,
            stdout="x" * 5000,
            stderr="dyld: missing native dependency",
        )

    with pytest.raises(RuntimeError, match="dyld: missing native dependency") as caught:
        verify_packaged_artifact.run_checked(
            ["artifact", "--version"],
            cwd=tmp_path,
            env={},
            timeout=1,
            runner=failed_runner,
        )
    assert len(str(caught.value)) < 4300


def test_every_github_action_is_pinned_to_a_full_commit_sha():
    failures = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match and not match.group(1).startswith("./"):
                reference = match.group(1)
                if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
                    failures.append(f"{workflow.name}:{line_number}:{reference}")
    assert failures == []


def test_candidate_and_publish_workflows_are_strictly_separated():
    candidate = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )
    publish = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in candidate
    assert "          - beta" in candidate
    assert 'tags: ["v*"]' not in candidate
    assert "scripts/build_release.py" in candidate
    assert "MACOS_SIGNING_IDENTITY" in candidate
    assert "signed candidates require Developer ID signing" in candidate
    assert "scripts/fetch_pinned_tool.py" in candidate
    assert "xvfb-run --auto-servernum python scripts/build_release.py --platform linux" in candidate
    assert '"cryptography>=50.0,<51.0"' in (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'metadata_args=(generate --directory' in candidate
    assert "pip-audit==2.10.1" in (ROOT / "requirements-build.txt").read_text(
        encoding="utf-8"
    )
    assert "Audit the installed Windows build environment" in candidate
    assert "Audit the installed macOS build environment" in candidate
    assert "Audit the installed Linux build environment" in candidate
    assert "--native-component openssl" in candidate
    assert "Get-AuthenticodeSignature" in candidate
    assert "WINDOWS_CERTIFICATE_PFX" in candidate
    assert "gpg --batch --verify" in candidate
    assert "LINUX_GPG_KEY_FINGERPRINT" in candidate
    assert "scripts/update_manifest_cli.py sign" in candidate
    assert "scripts/sign_artifact_manifest.py sign" in candidate
    assert "OAI_UPDATE_PRIVATE_KEY_B64" in candidate
    assert candidate.count("scripts/generate_update_trust.py") == 3
    assert "UPDATE_TRUST_PUBLIC_KEYS_JSON" in candidate
    assert "inputs.channel != 'candidate'" in candidate
    assert 'PYTHON_VERSION: "3.13.13"' in candidate
    assert candidate.count('-e ".[desktop]"') == 4
    assert "candidate-quality-evidence" in candidate
    assert "scripts/inventory_licenses.py" in candidate
    assert "scripts/run_quality_harness.py all --days 365 --projects 100 --hours 72" in candidate
    assert "clamav-freshclam" in candidate
    assert "sudo freshclam --verbose" in candidate
    assert "scripts/scan_artifacts.py --scanner clamscan" in candidate
    assert '"hdiutil", "attach"' in (
        ROOT / "scripts" / "build_release.py"
    ).read_text(encoding="utf-8")

    assert 'tags: ["v*"]' in publish
    assert "scripts/build_release.py" not in publish
    assert '--channel "$RELEASE_CHANNEL"' in publish
    assert "RELEASE_CHANNEL=beta" in publish
    assert "--prerelease" in publish
    assert "scripts/validate_channel_promotion.py" in publish
    assert "scripts/verify_release_reuse.py" in publish
    assert "gh release download" in publish
    assert "--draft" in publish
    assert "gh release upload" in publish
    assert "--allow-published-subset" in publish
    assert "--clobber" not in publish
    assert "update-channels" in publish
    assert '--tag "$GITHUB_REF_NAME"' in publish
    assert "head_sha=$env:GITHUB_SHA" in publish
    assert '"release-$env:RELEASE_CHANNEL-$env:GITHUB_SHA"' in publish
    assert 'release-${{ inputs.channel }}-${{ github.sha }}' in candidate
    assert "release-candidate-$env:GITHUB_SHA" not in publish
    assert "gh release create" in publish
    assert "scripts/update_manifest_cli.py verify" in publish
    assert "LINUX_GPG_PUBLIC_KEY_B64" in publish
    for forbidden in (
        "scripts/build_release.py",
        "scripts/scan_artifacts.py",
        "clamscan",
        "scripts/inventory_licenses.py",
        "scripts/run_quality_harness.py",
    ):
        assert forbidden not in publish


def test_ci_generates_supply_chain_and_full_quality_evidence():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/audit_dependencies.py" in workflow
    assert "scripts/generate_sbom.py" in workflow
    assert "scripts/inventory_licenses.py" in workflow
    assert "--fail-on-unknown" in workflow
    assert (
        "scripts/run_quality_harness.py all --days 365 --projects 100 --hours 72"
        in workflow
    )
    assert '-e ".[desktop]"' in workflow


def test_platform_artifact_names_and_installer_scope_are_stable():
    installer = (
        ROOT / "installer" / "windows" / "OpenAI-Free-Credit-Tracker.iss"
    ).read_text(encoding="utf-8")
    assert "windows-x86_64-setup" in installer
    assert "PrivilegesRequired=lowest" in installer
    linux = (ROOT / "packaging" / "linux" / "AppRun").read_text(encoding="utf-8")
    assert 'exec "$HERE/usr/bin/OpenAI-Free-Credit-Tracker" "$@"' in linux
    entitlements = (ROOT / "packaging" / "macos" / "entitlements.plist").read_text(
        encoding="utf-8"
    )
    assert "com.apple.security.cs.disable-library-validation" in entitlements
    assert "<false/>" in entitlements
