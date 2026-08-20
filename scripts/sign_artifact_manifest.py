"""Offline Ed25519 signing/verification for the aggregate artifact manifest."""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from quota_monitor.update_cli import (
    PRIVATE_KEY_ENV,
    PRIVATE_KEY_FD_ENV,
    _atomic_write_json,
    _read_credential,
    _read_json,
    _read_public_keyring,
)
from quota_monitor.update_crypto import Ed25519ManifestSigner, decode_base64_key
from quota_monitor.update_manifest import manifest_signing_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sign = commands.add_parser("sign")
    sign.add_argument("--input", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--key-id", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        document = _read_json(args.input)
        if args.command == "sign":
            signer = Ed25519ManifestSigner.from_private_bytes(
                decode_base64_key(
                    _read_credential(PRIVATE_KEY_ENV, PRIVATE_KEY_FD_ENV),
                    expected_bytes=32,
                    label="private signing key",
                )
            )
            _atomic_write_json(args.output, signer.sign(document, key_id=args.key_id))
            print("artifact manifest signed")
            return 0

        key_id = document.get("key_id")
        signature_text = document.get("signature")
        if not isinstance(key_id, str) or not isinstance(signature_text, str):
            raise ValueError("artifact manifest signature metadata is missing")
        signature = base64.b64decode(signature_text, validate=True)
        if len(signature) != 64:
            raise ValueError("artifact manifest Ed25519 signature must be 64 bytes")
        verifier = _read_public_keyring()
        from quota_monitor.update_crypto import build_ed25519_keyring

        if not build_ed25519_keyring(verifier).verify(
            key_id=key_id,
            message=manifest_signing_payload(document),
            signature=signature,
        ):
            raise ValueError("artifact manifest signature verification failed")
        print("artifact manifest verified")
        return 0
    except SystemExit:
        raise
    except Exception as error:
        print(f"artifact manifest operation failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
