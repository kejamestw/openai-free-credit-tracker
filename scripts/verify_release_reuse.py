"""Prove an existing GitHub Release is byte-identical to a candidate directory."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def _files(directory: Path) -> dict[str, Path]:
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("release comparison target must be a directory")
    result: dict[str, Path] = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError("release directories may contain only regular files")
        result[path.name] = path
    if not result:
        raise ValueError("release comparison directory is empty")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_reuse(
    candidate: Path,
    published: Path,
    *,
    allow_published_subset: bool = False,
) -> None:
    candidate_files = _files(candidate)
    published_files = _files(published)
    names_match = set(candidate_files) == set(published_files)
    is_subset = set(published_files) <= set(candidate_files)
    if not names_match and not (allow_published_subset and is_subset):
        missing = sorted(set(candidate_files) - set(published_files))
        extra = sorted(set(published_files) - set(candidate_files))
        raise ValueError(f"release asset names differ; missing={missing}, extra={extra}")
    for name in sorted(published_files):
        left = candidate_files[name]
        right = published_files[name]
        if left.stat().st_size != right.stat().st_size or _sha256(left) != _sha256(right):
            raise ValueError(f"release asset bytes differ: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--published", type=Path, required=True)
    parser.add_argument("--allow-published-subset", action="store_true")
    args = parser.parse_args()
    verify_reuse(
        args.candidate,
        args.published,
        allow_published_subset=args.allow_published_subset,
    )
    print("Existing GitHub Release assets are byte-identical to the candidate")


if __name__ == "__main__":
    main()
