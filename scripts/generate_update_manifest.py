"""Generate an unsigned update manifest from final distributable artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from quota_monitor.semver import SemVer

try:
    from scripts.release_metadata import classify
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from release_metadata import classify


DISTRIBUTABLE_FORMATS = frozenset(
    {"portable-exe", "inno-setup", "app-zip", "dmg", "tar-gzip", "appimage"}
)
# The consolidated v1 line is the first public package that contains the
# schema-v1 authenticated updater. Keep this independent from the target
# release version so RC1 can discover RC2 and later compatible releases.
MINIMUM_SUPPORTED_UPDATER_VERSION = "1.0.0-rc.1"


def generate(
    directory: Path,
    output: Path,
    *,
    version: str,
    source_epoch: int,
    repository: str,
    channel: str = "stable",
    minimum_updater_version: str = MINIMUM_SUPPORTED_UPDATER_VERSION,
) -> dict:
    directory = directory.resolve()
    if not repository or "/" not in repository:
        raise ValueError("repository must be an owner/name pair")
    parsed_version = SemVer.parse(version)
    parsed_minimum_updater = SemVer.parse(minimum_updater_version)
    if parsed_minimum_updater > parsed_version:
        raise ValueError("minimum updater version cannot exceed the target version")
    if channel == "stable" and parsed_version.is_prerelease:
        raise ValueError("stable update manifests require a final version")
    if channel == "beta" and not parsed_version.is_prerelease:
        raise ValueError("beta update manifests require a prerelease version")
    if channel not in {"stable", "beta"}:
        raise ValueError("update manifest channel must be beta or stable")
    artifacts = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            item = classify(path, version)
        except ValueError:
            continue
        if item["format"] not in DISTRIBUTABLE_FORMATS:
            continue
        artifacts.append(
            {
                "url": (
                    f"https://github.com/{repository}/releases/download/v{version}/"
                    f"{quote(path.name)}"
                ),
                "os": item["os"],
                "arch": item["arch"],
                "format": item["format"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
        )
    if len(artifacts) != 8:
        raise ValueError("update manifest requires all eight platform distributables")
    document = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "published_at": datetime.fromtimestamp(source_epoch, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "minimum_updater_version": str(parsed_minimum_updater),
        "artifacts": artifacts,
        "release_notes_url": f"https://github.com/{repository}/releases/tag/v{version}",
        "critical": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-epoch", type=int, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--channel", choices=("beta", "stable"), required=True)
    parser.add_argument(
        "--minimum-updater-version",
        default=MINIMUM_SUPPORTED_UPDATER_VERSION,
        help="oldest package version whose updater can consume this manifest",
    )
    args = parser.parse_args()
    generate(
        args.directory,
        args.output,
        version=args.version,
        source_epoch=args.source_epoch,
        repository=args.repository,
        channel=args.channel,
        minimum_updater_version=args.minimum_updater_version,
    )
    print(f"Unsigned update manifest generated: {args.output}")


if __name__ == "__main__":
    main()
