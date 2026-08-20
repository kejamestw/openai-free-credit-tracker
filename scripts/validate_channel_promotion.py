"""Validate a monotonic promotion of an already-signed beta channel manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quota_monitor.semver import SemVer


def _load(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("channel manifest is unreadable") from error
    if not isinstance(document, dict):
        raise ValueError("channel manifest must be an object")
    return document


def _beta_version(document: dict) -> SemVer:
    if document.get("schema_version") != 1 or document.get("channel") != "beta":
        raise ValueError("channel promotion accepts only schema-v1 beta manifests")
    if not isinstance(document.get("key_id"), str) or not isinstance(
        document.get("signature"), str
    ):
        raise ValueError("channel promotion requires signed manifests")
    version = SemVer.parse(document.get("version"))
    if not version.is_prerelease:
        raise ValueError("beta channel manifest must contain a prerelease version")
    return version


def validate_promotion(
    candidate: dict,
    current: dict | None = None,
    *,
    exact_match: bool | None = None,
) -> None:
    candidate_version = _beta_version(candidate)
    if current is None:
        return
    current_version = _beta_version(current)
    if candidate_version < current_version:
        raise ValueError("beta channel promotion must increase semantic version precedence")
    if candidate_version == current_version:
        identical = candidate == current if exact_match is None else exact_match
        if identical:
            return
        raise ValueError(
            "an equal-version beta promotion must be byte-identical to the current pointer"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--current", type=Path)
    args = parser.parse_args()
    candidate = _load(args.candidate)
    current = _load(args.current) if args.current is not None else None
    validate_promotion(
        candidate,
        current,
        exact_match=(
            args.candidate.read_bytes() == args.current.read_bytes()
            if args.current is not None
            else None
        ),
    )
    print("Beta channel promotion is monotonic")


if __name__ == "__main__":
    main()
