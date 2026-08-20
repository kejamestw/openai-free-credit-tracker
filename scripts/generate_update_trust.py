"""Generate the immutable public trust resource bundled by signed packages.

The updater signing seed is deliberately not accepted by this tool.  The only
credentials it reads are public Ed25519 keys used to authenticate the fixed
beta- and stable-channel manifest URLs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import quote


DEFAULT_REPOSITORY = "kejamestw/openai-free-credit-tracker"
CHANNEL_MANIFEST_NAME = "OpenAI-Free-Credit-Tracker-stable-update-manifest.json"
CHANNEL_MANIFEST_NAMES = {
    "stable": CHANNEL_MANIFEST_NAME,
    "beta": "OpenAI-Free-Credit-Tracker-beta-update-manifest.json",
}
PUBLIC_KEY_ENV = "UPDATE_SIGNING_PUBLIC_KEY_B64"
KEY_ID_ENV = "UPDATE_SIGNING_KEY_ID"
PUBLIC_KEYRING_ENV = "UPDATE_TRUST_PUBLIC_KEYS_JSON"
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TRUST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_urls",
        "manifest_hosts",
        "artifact_hosts",
        "release_notes_hosts",
        "public_keys",
    }
)
_GITHUB_DOWNLOAD_HOSTS = [
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
]


def stable_manifest_url(repository: str = DEFAULT_REPOSITORY) -> str:
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be a GitHub owner/name pair")
    return (
        f"https://github.com/{repository}/releases/latest/download/"
        f"{quote(CHANNEL_MANIFEST_NAME)}"
    )


def beta_manifest_url(repository: str = DEFAULT_REPOSITORY) -> str:
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be a GitHub owner/name pair")
    return f"https://raw.githubusercontent.com/{repository}/update-channels/beta.json"


def channel_manifest_url(channel: str, repository: str = DEFAULT_REPOSITORY) -> str:
    if channel == "stable":
        return stable_manifest_url(repository)
    if channel == "beta":
        return beta_manifest_url(repository)
    raise ValueError("trusted update channel must be beta or stable")


def channel_manifest_name(channel: str) -> str:
    try:
        return CHANNEL_MANIFEST_NAMES[channel]
    except KeyError:
        raise ValueError("signed release channel must be beta or stable") from None


def _canonical_public_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("the update signing public key is required")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("the update signing public key is not strict base64") from None
    if len(raw) != 32:
        raise ValueError("the update signing public key must be exactly 32 bytes")
    return base64.b64encode(raw).decode("ascii")


def _canonical_keyring(public_keys_b64: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(public_keys_b64, Mapping) or not 1 <= len(public_keys_b64) <= 4:
        raise ValueError("update trust must contain between one and four public keys")
    keys: dict[str, str] = {}
    for key_id, public_key in public_keys_b64.items():
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ValueError("update trust contains an invalid key ID")
        keys[key_id] = _canonical_public_key(public_key)
    return {key_id: keys[key_id] for key_id in sorted(keys)}


def build_document(
    *,
    key_id: str,
    public_key_b64: str,
    repository: str,
    public_keys_b64: Mapping[str, str] | None = None,
) -> dict:
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise ValueError("the update signing key ID is invalid")
    active_public_key = _canonical_public_key(public_key_b64)
    keys = _canonical_keyring(
        public_keys_b64 if public_keys_b64 is not None else {key_id: active_public_key}
    )
    if keys.get(key_id) != active_public_key:
        raise ValueError("active update signing key is missing or mismatched in the trust keyring")
    return {
        "schema_version": 1,
        "manifest_urls": {
            channel: channel_manifest_url(channel, repository)
            for channel in ("beta", "stable")
        },
        "manifest_hosts": list(_GITHUB_DOWNLOAD_HOSTS) + ["raw.githubusercontent.com"],
        "artifact_hosts": list(_GITHUB_DOWNLOAD_HOSTS),
        "release_notes_hosts": ["github.com"],
        "public_keys": keys,
    }


def validate_document(
    document: object,
    *,
    repository: str = DEFAULT_REPOSITORY,
) -> dict:
    if not isinstance(document, dict) or set(document) != _TRUST_FIELDS:
        raise ValueError("update trust must contain exactly the schema-v1 fields")
    if document.get("schema_version") != 1:
        raise ValueError("update trust schema_version must be 1")
    expected_urls = {
        channel: channel_manifest_url(channel, repository)
        for channel in ("beta", "stable")
    }
    if document.get("manifest_urls") != expected_urls:
        raise ValueError("update trust does not contain both fixed signed-channel URLs")
    expected_manifest_hosts = list(_GITHUB_DOWNLOAD_HOSTS) + ["raw.githubusercontent.com"]
    if document.get("manifest_hosts") != expected_manifest_hosts:
        raise ValueError("update trust manifest host allowlist is invalid")
    if document.get("artifact_hosts") != _GITHUB_DOWNLOAD_HOSTS:
        raise ValueError("update trust artifact host allowlist is invalid")
    if document.get("release_notes_hosts") != ["github.com"]:
        raise ValueError("update trust release-notes host allowlist is invalid")
    keys = document.get("public_keys")
    if not isinstance(keys, dict):
        raise ValueError("update trust public_keys must be an object")
    if _canonical_keyring(keys) != keys:
        raise ValueError("update trust public keyring is not canonical")
    return document


def load_and_validate(
    path: Path,
    *,
    repository: str = DEFAULT_REPOSITORY,
) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("update trust resource is unreadable") from error
    return validate_document(document, repository=repository)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate public keyring field")
        result[key] = value
    return result


def public_keyring_from_environment() -> Mapping[str, str] | None:
    encoded = os.environ.get(PUBLIC_KEYRING_ENV, "").strip()
    if not encoded:
        return None
    try:
        document = json.loads(encoded, object_pairs_hook=_unique_object)
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("protected update trust keyring JSON is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("protected update trust keyring must be an object")
    return document


def generate(
    output: Path,
    *,
    key_id: str,
    public_key_b64: str,
    repository: str = DEFAULT_REPOSITORY,
    public_keys_b64: Mapping[str, str] | None = None,
) -> dict:
    document = build_document(
        key_id=key_id,
        public_key_b64=public_key_b64,
        repository=repository,
        public_keys_b64=public_keys_b64,
    )
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    output = output.resolve()
    if output.exists():
        if not output.is_file() or output.read_text(encoding="utf-8") != encoded:
            raise ValueError("refusing to replace a different update trust resource")
        return document
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8", newline="\n")
    load_and_validate(output, repository=repository)
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    args = parser.parse_args()
    key_id = os.environ.get(KEY_ID_ENV, "")
    public_key = os.environ.get(PUBLIC_KEY_ENV, "")
    document = generate(
        args.output,
        key_id=key_id,
        public_key_b64=public_key,
        repository=args.repository,
        public_keys_b64=public_keyring_from_environment(),
    )
    print(
        "Immutable update trust generated: "
        f"{args.output} ({next(iter(document['public_keys']))})"
    )


if __name__ == "__main__":
    main()
