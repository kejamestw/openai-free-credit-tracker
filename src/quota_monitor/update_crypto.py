"""Production Ed25519 signing and verification for update manifests."""

from __future__ import annotations

import base64
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .update_manifest import (
    Ed25519KeyringVerifier,
    ManifestValidationError,
    manifest_signing_payload,
)


_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CryptographyEd25519Backend:
    """Ed25519 backend implemented by the maintained ``cryptography`` package."""

    def verify(
        self,
        *,
        public_key: bytes,
        message: bytes,
        signature: bytes,
    ) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True


@dataclass(frozen=True, repr=False)
class Ed25519ManifestSigner:
    """Hold a raw private seed without exposing it through repr or exceptions."""

    _private_key: Ed25519PrivateKey

    @classmethod
    def from_private_bytes(cls, private_key: bytes) -> "Ed25519ManifestSigner":
        if not isinstance(private_key, bytes) or len(private_key) != 32:
            raise ValueError("Ed25519 private key seed must be exactly 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(private_key))

    @property
    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign(self, document: Mapping[str, Any], *, key_id: str) -> dict[str, Any]:
        ensure_manifest_can_be_signed(document)
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ValueError("key_id has an invalid format")
        signed = deepcopy(dict(document))
        signed["key_id"] = key_id
        signed["signature"] = ""
        payload = manifest_signing_payload(signed)
        signed["signature"] = base64.b64encode(
            self._private_key.sign(payload)
        ).decode("ascii")
        return signed

    def __repr__(self) -> str:
        return "Ed25519ManifestSigner(<redacted>)"


def build_ed25519_keyring(
    public_keys: Mapping[str, bytes],
) -> Ed25519KeyringVerifier:
    keys = dict(public_keys)
    if not keys:
        raise ValueError("at least one Ed25519 public key is required")
    for key_id, public_key in keys.items():
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("public key IDs must be non-empty strings")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError(f"public key {key_id!r} must be exactly 32 bytes")
    return Ed25519KeyringVerifier(keys, CryptographyEd25519Backend())


def decode_base64_key(value: str, *, expected_bytes: int, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is unavailable")
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError(f"{label} is not valid base64") from None
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} must decode to {expected_bytes} bytes")
    return decoded


def ensure_manifest_can_be_signed(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise ManifestValidationError("manifest must be an object")
    forbidden = {"private_key", "private_key_b64", "signing_key", "secret_key"}

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            if any(str(key).lower() in forbidden for key in value):
                raise ManifestValidationError(
                    "private signing material is forbidden in manifests"
                )
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(document)
