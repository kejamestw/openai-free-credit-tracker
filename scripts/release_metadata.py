"""Create and verify deterministic checksums and a release artifact manifest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from quota_monitor.semver import SemVer

try:
    from scripts.generate_update_trust import channel_manifest_name
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from generate_update_trust import channel_manifest_name  # type: ignore[no-redef]


MANIFEST_NAME = "artifact-manifest.json"
CHECKSUM_NAME = "SHA256SUMS.txt"
IGNORED_NAMES = frozenset({MANIFEST_NAME, CHECKSUM_NAME})
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
ARTIFACT_PATTERN = re.compile(
    r"(?P<os>windows|macos|linux)-(?P<arch>x86_64|arm64)"
    r"(?P<suffix>-.+|\..+)\Z"
)
CORE_RELEASE_SLOTS = frozenset(
    {
        ("windows", "x86_64", "portable-exe"),
        ("windows", "x86_64", "inno-setup"),
        ("windows", "x86_64", "cyclonedx-json"),
        ("macos", "x86_64", "app-zip"),
        ("macos", "x86_64", "dmg"),
        ("macos", "x86_64", "cyclonedx-json"),
        ("macos", "arm64", "app-zip"),
        ("macos", "arm64", "dmg"),
        ("macos", "arm64", "cyclonedx-json"),
        ("linux", "x86_64", "tar-gzip"),
        ("linux", "x86_64", "appimage"),
        ("linux", "x86_64", "cyclonedx-json"),
    }
)
STABLE_SIGNING_SLOTS = frozenset(
    {
        ("linux", "x86_64", "openpgp-tar-signature"),
        ("linux", "x86_64", "openpgp-appimage-signature"),
        ("all", "all", "update-manifest"),
        ("all", "all", "update-channel-manifest"),
    }
)
CANDIDATE_METADATA_SLOTS = frozenset({("all", "all", "update-manifest-unsigned")})
QUALITY_EVIDENCE_SLOTS = frozenset(
    {
        ("all", "all", "dependency-audit"),
        ("all", "all", "license-inventory"),
        ("all", "all", "quality-evidence"),
        ("all", "all", "malware-scan"),
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generated_at() -> str:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def classify(path: Path, version: str) -> dict[str, object]:
    prefix = f"OpenAI-Free-Credit-Tracker-{version}-"
    if path.name in {channel_manifest_name("stable"), channel_manifest_name("beta")}:
        return {
            "filename": path.name,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "os": "all",
            "arch": "all",
            "format": "update-channel-manifest",
        }
    evidence_formats = {
        "dependency-audit.json": "dependency-audit",
        "license-inventory.json": "license-inventory",
        "quality-evidence.json": "quality-evidence",
        "malware-scan.json": "malware-scan",
    }
    for suffix_name, artifact_format in evidence_formats.items():
        if path.name == f"{prefix}{suffix_name}":
            return {
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "os": "all",
                "arch": "all",
                "format": artifact_format,
            }
    if path.name == f"{prefix}update-manifest.json":
        return {
            "filename": path.name,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "os": "all",
            "arch": "all",
            "format": "update-manifest",
        }
    if path.name == f"{prefix}update-manifest.unsigned.json":
        return {
            "filename": path.name,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "os": "all",
            "arch": "all",
            "format": "update-manifest-unsigned",
        }
    match = ARTIFACT_PATTERN.fullmatch(path.name.removeprefix(prefix))
    if not path.name.startswith(prefix) or match is None:
        raise ValueError(f"artifact does not follow the release naming contract: {path.name}")
    suffix = match.group("suffix")
    if suffix.endswith(".tar.gz.asc"):
        artifact_format = "openpgp-tar-signature"
    elif suffix.endswith(".AppImage.asc"):
        artifact_format = "openpgp-appimage-signature"
    elif suffix.endswith("portable.exe"):
        artifact_format = "portable-exe"
    elif suffix.endswith("setup.exe"):
        artifact_format = "inno-setup"
    elif suffix.endswith(".app.zip"):
        artifact_format = "app-zip"
    elif suffix.endswith(".dmg"):
        artifact_format = "dmg"
    elif suffix.endswith(".AppImage"):
        artifact_format = "appimage"
    elif suffix.endswith(".tar.gz"):
        artifact_format = "tar-gzip"
    elif suffix.endswith(".cdx.json"):
        artifact_format = "cyclonedx-json"
    else:
        raise ValueError(f"unsupported release artifact: {path.name}")
    return {
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "os": match.group("os"),
        "arch": match.group("arch"),
        "format": artifact_format,
    }


def _validate_release_set(artifacts: list[dict[str, object]], channel: str) -> None:
    actual = {
        (str(item["os"]), str(item["arch"]), str(item["format"])) for item in artifacts
    }
    expected = CORE_RELEASE_SLOTS | QUALITY_EVIDENCE_SLOTS | (
        STABLE_SIGNING_SLOTS if channel in {"beta", "stable"} else CANDIDATE_METADATA_SLOTS
    )
    if actual != expected or len(artifacts) != len(expected):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"release artifact set mismatch; missing={missing}, extra={extra}")


def _validate_channel_manifest(directory: Path, version: str, channel: str) -> None:
    versioned_path = directory / f"OpenAI-Free-Credit-Tracker-{version}-update-manifest.json"
    if channel == "candidate":
        if any((directory / channel_manifest_name(item)).exists() for item in ("beta", "stable")):
            raise ValueError("unsigned candidates cannot contain the stable channel manifest")
        return
    channel_path = directory / channel_manifest_name(channel)
    if not channel_path.is_file() or not versioned_path.is_file():
        raise ValueError("stable release requires versioned and fixed-channel update manifests")
    if channel_path.read_bytes() != versioned_path.read_bytes():
        raise ValueError("fixed-channel update manifest is not byte-identical to the versioned manifest")


def _validate_channel_version(version: str, channel: str) -> None:
    parsed = SemVer.parse(version)
    if channel == "beta" and not parsed.is_prerelease:
        raise ValueError("beta release metadata requires a prerelease version")
    if channel == "stable" and parsed.is_prerelease:
        raise ValueError("stable release metadata requires a final version")


def _load_evidence(directory: Path, version: str, suffix: str) -> object:
    path = directory / f"OpenAI-Free-Credit-Tracker-{version}-{suffix}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"release evidence is unreadable: {path.name}") from error
    if not isinstance(document, dict) and suffix != "dependency-audit":
        raise ValueError(f"release evidence must be an object: {path.name}")
    return document


def _validate_evidence(
    directory: Path, version: str, artifacts: list[dict[str, object]]
) -> None:
    license_inventory = _load_evidence(directory, version, "license-inventory")
    if not isinstance(license_inventory, dict):
        raise ValueError("license inventory evidence must be an object")
    summary = license_inventory.get("summary", {})
    packages = license_inventory.get("packages")
    if (
        license_inventory.get("schema_version") != 1
        or not isinstance(summary, dict)
        or not isinstance(packages, list)
        or not packages
        or summary.get("package_count") != len(packages)
        or summary.get("unknown_license_count") != 0
        or summary.get("unknown_license_packages") != []
    ):
        raise ValueError("license inventory is incomplete or contains unknown licenses")

    quality = _load_evidence(directory, version, "quality-evidence")
    if not isinstance(quality, dict):
        raise ValueError("quality evidence must be an object")
    reports = quality.get("reports")
    if quality.get("schema_version") != 1 or quality.get("passed") is not True:
        raise ValueError("quality evidence did not pass")
    if not isinstance(reports, list):
        raise ValueError("quality evidence reports are missing")
    performance = next(
        (item for item in reports if isinstance(item, dict) and item.get("kind") == "synthetic_performance"),
        None,
    )
    soak = next(
        (item for item in reports if isinstance(item, dict) and item.get("kind") == "accelerated_simulated_soak"),
        None,
    )
    performance_scenario = performance.get("scenario") if performance else None
    soak_scenario = soak.get("scenario") if soak else None
    if (
        not performance
        or performance.get("passed") is not True
        or not isinstance(performance_scenario, dict)
        or performance_scenario.get("days") != 365
        or performance_scenario.get("projects") != 100
        or not soak
        or soak.get("passed") is not True
        or not isinstance(soak_scenario, dict)
        or soak_scenario.get("simulated_hours") != 72
    ):
        raise ValueError("quality evidence does not contain the full performance/soak scenarios")

    dependency_audit = _load_evidence(directory, version, "dependency-audit")
    if not isinstance(dependency_audit, dict):
        raise ValueError("dependency audit evidence must be an object")
    dependencies = dependency_audit.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("dependency audit evidence is incomplete or contains vulnerabilities")
    for item in dependencies:
        if not isinstance(item, dict):
            raise ValueError("dependency audit evidence is incomplete or contains vulnerabilities")
        if item.get("name") == "openai-free-credit-tracker" and item.get(
            "skip_reason"
        ) == "distribution marked as editable":
            continue
        if item.get("skip_reason") or not isinstance(item.get("vulns"), list) or item["vulns"]:
            raise ValueError("dependency audit evidence is incomplete or contains vulnerabilities")

    malware = _load_evidence(directory, version, "malware-scan")
    if not isinstance(malware, dict):
        raise ValueError("malware scan evidence must be an object")
    if (
        malware.get("schema_version") != 1
        or malware.get("scanner") != "clamscan"
        or not isinstance(malware.get("scanner_version"), str)
        or not malware["scanner_version"].startswith("ClamAV ")
        or "/" not in malware["scanner_version"]
        or malware.get("passed") is not True
        or not isinstance(malware.get("artifacts"), list)
    ):
        raise ValueError("malware scan evidence is invalid")
    expected = {
        str(item["filename"]): (item["size"], item["sha256"])
        for item in artifacts
        if item["format"] != "malware-scan"
    }
    scanned = {}
    for item in malware["artifacts"]:
        if not isinstance(item, dict) or item.get("status") != "clean":
            raise ValueError("malware scan contains an invalid artifact result")
        name = item.get("name")
        if not isinstance(name, str) or name in scanned:
            raise ValueError("malware scan contains an unsafe or duplicate artifact name")
        scanned[name] = (item.get("size"), item.get("sha256"))
    if scanned != expected:
        raise ValueError("malware scan does not cover the complete immutable artifact set")


def _security_metadata(
    *,
    channel: str,
    repository: str,
    run_id: str,
    windows_identity: str | None,
    macos_identity: str | None,
    linux_fingerprint: str | None,
    update_key_id: str | None,
) -> tuple[dict, dict]:
    if not repository or "/" not in repository or not run_id.isdigit():
        raise ValueError("repository and numeric GitHub Actions run_id are required")
    verified = channel in {"beta", "stable"}
    values = (windows_identity, macos_identity, linux_fingerprint, update_key_id)
    if verified and not all(values):
        raise ValueError("stable release metadata requires every signing identity")
    if not verified and any(values):
        raise ValueError("candidate release metadata must be explicitly unsigned")
    if linux_fingerprint and not re.fullmatch(r"[0-9A-Fa-f]{40,64}", linux_fingerprint):
        raise ValueError("Linux signing fingerprint must contain 40 to 64 hex characters")
    if update_key_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", update_key_id):
        raise ValueError("update signing key ID is invalid")
    status = "verified" if verified else "unsigned"
    signing = {
        "windows": {
            "status": status,
            "method": "authenticode-sha256",
            **({"identity": windows_identity} if windows_identity else {}),
        },
        "macos": {
            "status": status,
            "method": "developer-id-hardened-runtime-notarization",
            **({"identity": macos_identity} if macos_identity else {}),
        },
        "linux": {
            "status": status,
            "method": "openpgp-detached",
            **({"fingerprint": linux_fingerprint.upper()} if linux_fingerprint else {}),
        },
        "update_manifest": {
            "status": status,
            "method": "ed25519",
            **({"key_id": update_key_id} if update_key_id else {}),
        },
    }
    provenance = {
        "builder": "github-actions",
        "repository": repository,
        "workflow": "release-candidate.yml",
        "run_id": run_id,
    }
    return signing, provenance


def _validate_security_metadata(manifest: dict, channel: str) -> None:
    signing = manifest.get("signing")
    provenance = manifest.get("provenance")
    if not isinstance(signing, dict) or set(signing) != {
        "windows",
        "macos",
        "linux",
        "update_manifest",
    }:
        raise ValueError("manifest signing metadata is incomplete")
    expected_status = "verified" if channel in {"beta", "stable"} else "unsigned"
    for platform_name, value in signing.items():
        if not isinstance(value, dict) or value.get("status") != expected_status:
            raise ValueError(f"manifest signing status is invalid for {platform_name}")
    if channel in {"beta", "stable"}:
        if not signing["windows"].get("identity") or not signing["macos"].get("identity"):
            raise ValueError("stable manifest is missing code-signing identities")
        fingerprint = signing["linux"].get("fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9A-F]{40,64}", fingerprint):
            raise ValueError("stable manifest has an invalid Linux signing fingerprint")
        key_id = signing["update_manifest"].get("key_id")
        if not isinstance(key_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key_id
        ):
            raise ValueError("stable manifest has an invalid update signing key ID")
        if manifest.get("key_id") != key_id:
            raise ValueError("artifact manifest signing key ID does not match metadata")
        signature_text = manifest.get("signature")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except (TypeError, ValueError, base64.binascii.Error):
            raise ValueError("artifact manifest signature is not strict base64") from None
        if len(signature) != 64:
            raise ValueError("artifact manifest Ed25519 signature must be 64 bytes")
    elif "signature" in manifest or "key_id" in manifest:
        raise ValueError("unsigned candidate manifest cannot contain signature metadata")
    if (
        not isinstance(provenance, dict)
        or provenance.get("builder") != "github-actions"
        or provenance.get("workflow") != "release-candidate.yml"
        or not isinstance(provenance.get("repository"), str)
        or "/" not in provenance["repository"]
        or not isinstance(provenance.get("run_id"), str)
        or not provenance["run_id"].isdigit()
    ):
        raise ValueError("manifest provenance is invalid")


def generate(
    directory: Path,
    *,
    version: str,
    source_commit: str,
    channel: str,
    repository: str = "local/repository",
    run_id: str = "0",
    windows_identity: str | None = None,
    macos_identity: str | None = None,
    linux_fingerprint: str | None = None,
    update_key_id: str | None = None,
) -> dict:
    directory = directory.resolve()
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("source_commit must be a full lowercase 40-character commit SHA")
    if channel not in {"candidate", "beta", "stable"}:
        raise ValueError("channel must be candidate, beta, or stable")
    _validate_channel_version(version, channel)
    _validate_channel_manifest(directory, version, channel)
    files = sorted(
        path for path in directory.iterdir() if path.is_file() and path.name not in IGNORED_NAMES
    )
    if not files:
        raise ValueError("release directory contains no artifacts")
    artifacts = [classify(path, version) for path in files]
    _validate_release_set(artifacts, channel)
    _validate_evidence(directory, version, artifacts)
    signing, provenance = _security_metadata(
        channel=channel,
        repository=repository,
        run_id=run_id,
        windows_identity=windows_identity,
        macos_identity=macos_identity,
        linux_fingerprint=linux_fingerprint,
        update_key_id=update_key_id,
    )
    manifest = {
        "schema_version": 1,
        "package_version": version,
        "source_commit": source_commit,
        "channel": channel,
        "generated_at": _generated_at(),
        "artifacts": artifacts,
        "signing": signing,
        "provenance": provenance,
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (directory / CHECKSUM_NAME).write_text(
        "".join(f"{item['sha256']}  {item['filename']}\n" for item in artifacts),
        encoding="ascii",
        newline="\n",
    )
    return manifest


def verify(
    directory: Path,
    *,
    version: str,
    source_commit: str,
    channel: str,
    tag: str | None = None,
) -> dict:
    directory = directory.resolve()
    _validate_channel_version(version, channel)
    manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported artifact manifest schema")
    expected = {
        "package_version": version,
        "source_commit": source_commit,
        "channel": channel,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"manifest {key} does not match the release request")
    _validate_channel_manifest(directory, version, channel)
    _validate_security_metadata(manifest, channel)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not equal package version v{version}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        raise ValueError("manifest artifacts must be a list of objects")
    listed = []
    checksum_lines = []
    for item in artifacts:
        filename = item.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("manifest contains an unsafe artifact filename")
        path = directory / filename
        if path.resolve().parent != directory or not path.is_file():
            raise ValueError(f"artifact path is unsafe or missing: {filename}")
        if path.stat().st_size != item.get("size"):
            raise ValueError(f"artifact size mismatch: {filename}")
        actual_hash = sha256_file(path)
        if actual_hash != item.get("sha256"):
            raise ValueError(f"artifact checksum mismatch: {filename}")
        if classify(path, version) != item:
            raise ValueError(f"artifact classification mismatch: {filename}")
        listed.append(filename)
        checksum_lines.append(f"{actual_hash}  {filename}\n")

    _validate_release_set(artifacts, channel)
    _validate_evidence(directory, version, artifacts)
    if listed != sorted(listed):
        raise ValueError("manifest artifacts are not in canonical filename order")

    actual_files = sorted(
        path.name for path in directory.iterdir() if path.is_file() and path.name not in IGNORED_NAMES
    )
    if sorted(listed) != actual_files or not actual_files:
        raise ValueError("release directory and manifest artifact list differ")
    if (directory / CHECKSUM_NAME).read_text(encoding="ascii") != "".join(checksum_lines):
        raise ValueError("SHA256SUMS.txt does not match the artifact manifest")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("generate", "verify"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--channel", choices=("candidate", "beta", "stable"), required=True)
    parser.add_argument("--tag")
    parser.add_argument("--repository", default="local/repository")
    parser.add_argument("--run-id", default="0")
    parser.add_argument("--windows-identity")
    parser.add_argument("--macos-identity")
    parser.add_argument("--linux-fingerprint")
    parser.add_argument("--update-key-id")
    args = parser.parse_args()
    operation = generate if args.mode == "generate" else verify
    kwargs = {
        "version": args.version,
        "source_commit": args.source_commit,
        "channel": args.channel,
    }
    if args.mode == "verify":
        kwargs["tag"] = args.tag
    else:
        kwargs.update(
            {
                "repository": args.repository,
                "run_id": args.run_id,
                "windows_identity": args.windows_identity,
                "macos_identity": args.macos_identity,
                "linux_fingerprint": args.linux_fingerprint,
                "update_key_id": args.update_key_id,
            }
        )
    operation(args.directory, **kwargs)
    print(f"Release metadata {args.mode} passed: {args.directory}")


if __name__ == "__main__":
    main()
