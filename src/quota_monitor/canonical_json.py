"""Canonical JSON for the RFC 8785-compatible update-manifest value subset."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


MAX_SAFE_INTEGER = 9_007_199_254_740_991


class CanonicalizationError(ValueError):
    pass


def canonicalize_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes suitable for signing.

    Update manifests deliberately permit only null, booleans, strings, arrays,
    objects, and I-JSON safe integers. That subset is serialized exactly as JCS
    (RFC 8785). Floating-point values are rejected so Python and ECMAScript number
    formatting can never disagree at a signature boundary.
    """

    return _serialize(value).encode("utf-8")


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError("integer is outside the I-JSON safe range")
        return str(value)
    if isinstance(value, float):
        raise CanonicalizationError("floating-point values are not allowed")
    if isinstance(value, str):
        _validate_unicode(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        keys = list(value.keys())
        if any(not isinstance(key, str) for key in keys):
            raise CanonicalizationError("object keys must be strings")
        for key in keys:
            _validate_unicode(key)
        ordered = sorted(keys, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(
            f"{_serialize(key)}:{_serialize(value[key])}" for key in ordered
        ) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    raise CanonicalizationError(f"unsupported JSON value: {type(value).__name__}")


def _validate_unicode(value: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CanonicalizationError("strings must not contain lone surrogates") from error
