import base64
import json
import os
from datetime import datetime, timezone

import pytest

from quota_monitor.update_cli import build_parser, main
from quota_monitor.update_crypto import Ed25519ManifestSigner, build_ed25519_keyring
from quota_monitor.update_manifest import (
    ManifestParser,
    ManifestPolicy,
    ManifestSignatureError,
)


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "update")
NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def load_fixture(name="stable-windows.json"):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as source:
        return json.load(source)


def load_case(name):
    return load_fixture(name)


def policy():
    return ManifestPolicy(
        allowed_artifact_hosts={"downloads.example.com"},
        allowed_release_notes_hosts={"releases.example.com"},
    )


def test_production_ed25519_round_trip_tampering_and_key_rotation():
    old = Ed25519ManifestSigner.from_private_bytes(os.urandom(32))
    current = Ed25519ManifestSigner.from_private_bytes(os.urandom(32))
    signed = current.sign(load_fixture(), key_id="current")
    parser = ManifestParser(
        policy=policy(),
        verifier=build_ed25519_keyring(
            {"old": old.public_key_bytes, "current": current.public_key_bytes}
        ),
        now=lambda: NOW,
    )

    assert str(parser.parse(signed).version) == "0.3.0"
    tampered = load_case("adversarial-cases.json")["tampered"]
    signed[tampered["mutate_after_sign"]] = "0.3.1"
    with pytest.raises(ManifestSignatureError):
        parser.parse(signed)
    unknown = current.sign(load_fixture(), key_id="next")
    with pytest.raises(ManifestSignatureError):
        parser.parse(unknown)
    assert "redacted" in repr(current).lower()


def test_offline_cli_accepts_private_key_from_environment_without_argv_leak(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "manifest.json"
    output = tmp_path / "signed.json"
    source.write_text(json.dumps(load_fixture()), encoding="utf-8")
    private = os.urandom(32)
    encoded_private = base64.b64encode(private).decode("ascii")
    monkeypatch.setenv("OAI_UPDATE_PRIVATE_KEY_B64", encoded_private)

    args = [
        "sign", "--input", str(source), "--output", str(output),
        "--key-id", "release-2026", "--artifact-host", "downloads.example.com",
        "--release-notes-host", "releases.example.com",
    ]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert encoded_private not in captured.out + captured.err
    assert "private-key" not in build_parser().format_help()

    signer = Ed25519ManifestSigner.from_private_bytes(private)
    monkeypatch.delenv("OAI_UPDATE_PRIVATE_KEY_B64")
    monkeypatch.setenv(
        "OAI_UPDATE_PUBLIC_KEYS_JSON",
        json.dumps({"release-2026": base64.b64encode(signer.public_key_bytes).decode("ascii")}),
    )
    assert main([
        "verify", "--input", str(output), "--artifact-host", "downloads.example.com",
        "--release-notes-host", "releases.example.com",
    ]) == 0


def test_offline_cli_accepts_private_key_from_inherited_fd(tmp_path, monkeypatch):
    source = tmp_path / "manifest.json"
    output = tmp_path / "signed.json"
    key_file = tmp_path / "inherited-key"
    source.write_text(json.dumps(load_fixture()), encoding="utf-8")
    key_file.write_text(base64.b64encode(os.urandom(32)).decode("ascii"), encoding="ascii")
    descriptor = os.open(key_file, os.O_RDONLY)
    try:
        monkeypatch.setenv("OAI_UPDATE_PRIVATE_KEY_FD", str(descriptor))
        assert main([
            "sign", "--input", str(source), "--output", str(output),
            "--key-id", "fd-key", "--artifact-host", "downloads.example.com",
            "--release-notes-host", "releases.example.com",
        ]) == 0
    finally:
        os.close(descriptor)
    assert output.is_file()
