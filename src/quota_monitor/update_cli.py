"""Offline signing and verification CLI for update manifests.

Private key bytes are accepted only through an environment value or inherited
file descriptor. They are never accepted on argv or included in diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .update_crypto import (
    Ed25519ManifestSigner,
    build_ed25519_keyring,
    decode_base64_key,
)
from .update_manifest import DEFAULT_MAX_MANIFEST_BYTES, ManifestParser, ManifestPolicy


PRIVATE_KEY_ENV = "OAI_UPDATE_PRIVATE_KEY_B64"
PRIVATE_KEY_FD_ENV = "OAI_UPDATE_PRIVATE_KEY_FD"
PUBLIC_KEYS_ENV = "OAI_UPDATE_PUBLIC_KEYS_JSON"
PUBLIC_KEYS_FD_ENV = "OAI_UPDATE_PUBLIC_KEYS_FD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign or verify an update manifest offline")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("sign", "verify"):
        child = subcommands.add_parser(command)
        child.add_argument("--input", required=True, type=Path)
        child.add_argument("--artifact-host", action="append", required=True)
        child.add_argument("--release-notes-host", action="append")
        if command == "sign":
            child.add_argument("--output", required=True, type=Path)
            child.add_argument("--key-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "sign":
            manifest = _read_json(arguments.input)
            signer = Ed25519ManifestSigner.from_private_bytes(
                decode_base64_key(
                    _read_credential(PRIVATE_KEY_ENV, PRIVATE_KEY_FD_ENV),
                    expected_bytes=32,
                    label="private signing key",
                )
            )
            signed = signer.sign(manifest, key_id=arguments.key_id)
            _parse(
                signed,
                public_keys={arguments.key_id: signer.public_key_bytes},
                artifact_hosts=arguments.artifact_host,
                release_hosts=arguments.release_notes_host,
            )
            _atomic_write_json(arguments.output, signed)
            print("manifest signed")
            return 0
        public_keys = _read_public_keyring()
        parsed = _parse(
            _read_bytes(arguments.input),
            public_keys=public_keys,
            artifact_hosts=arguments.artifact_host,
            release_hosts=arguments.release_notes_host,
        )
        print(f"manifest verified: {parsed.version}")
        return 0
    except SystemExit:
        raise
    except Exception as error:
        print(f"manifest operation failed: {type(error).__name__}", file=sys.stderr)
        return 1


def _parse(document, *, public_keys, artifact_hosts, release_hosts):
    policy = ManifestPolicy(
        allowed_artifact_hosts=frozenset(artifact_hosts),
        allowed_release_notes_hosts=(
            frozenset(release_hosts) if release_hosts else frozenset(artifact_hosts)
        ),
    )
    return ManifestParser(
        policy=policy,
        verifier=build_ed25519_keyring(public_keys),
    ).parse(document)


def _read_credential(value_name: str, fd_name: str) -> str:
    value = os.environ.get(value_name)
    descriptor = os.environ.get(fd_name)
    if bool(value) == bool(descriptor):
        raise ValueError("exactly one protected credential source is required")
    if value:
        if len(value.encode("utf-8")) > 64 * 1024:
            raise ValueError("credential input exceeds the maximum size")
        return value
    try:
        fd = int(descriptor or "")
        if fd < 0:
            raise ValueError
    except ValueError:
        raise ValueError("credential file descriptor is invalid") from None
    with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
        content = stream.read(64 * 1024 + 1)
    if len(content) > 64 * 1024:
        raise ValueError("credential input exceeds the maximum size")
    return content.decode("ascii", errors="strict").strip()


def _read_public_keyring() -> dict[str, bytes]:
    raw = _read_credential(PUBLIC_KEYS_ENV, PUBLIC_KEYS_FD_ENV)
    document = json.loads(raw)
    if not isinstance(document, dict) or not document:
        raise ValueError("public keyring must be a non-empty object")
    return {
        key_id: decode_base64_key(value, expected_bytes=32, label="public key")
        for key_id, value in document.items()
    }


def _read_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    if len(content) > DEFAULT_MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the maximum size")
    return content


def _read_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("manifest contains a duplicate field")
            result[key] = value
        return result

    document = json.loads(_read_bytes(path), object_pairs_hook=reject_duplicates)
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    return document


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
