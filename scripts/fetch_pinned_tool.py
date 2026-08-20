"""Fetch a release tool by immutable asset ID and verify its published SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "packaging" / "linux" / "toolchain.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_tool(config_path: Path, name: str) -> dict:
    document = json.loads(config_path.read_text(encoding="utf-8"))
    tool = document.get("tools", {}).get(name)
    if not isinstance(tool, dict):
        raise ValueError(f"unknown pinned tool: {name}")
    url = tool.get("url")
    digest = tool.get("sha256")
    size = tool.get("size")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValueError("pinned tool URL must use the GitHub asset API over HTTPS")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("pinned tool must declare a SHA-256 digest")
    if not isinstance(size, int) or size < 1:
        raise ValueError("pinned tool must declare a positive byte size")
    return tool


def verify_file(path: Path, tool: dict) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == tool["size"]
        and _sha256(path) == tool["sha256"]
    )


def fetch(config_path: Path, name: str, output: Path) -> Path:
    tool = load_tool(config_path, name)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if verify_file(output, tool):
        return output

    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "OpenAI-Free-Credit-Tracker-release-builder",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(tool["url"], headers=headers)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".download", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, urllib.request.urlopen(
            request, timeout=60
        ) as response:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        if not verify_file(temporary, tool):
            raise RuntimeError(f"downloaded {name} failed pinned size/SHA-256 validation")
        os.replace(temporary, output)
        output.chmod(output.stat().st_mode | 0o111)
        return output
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=("appimagetool-x86_64", "runtime-x86_64"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = fetch(args.config, args.name, args.output)
    print(f"Pinned tool ready: {result}")


if __name__ == "__main__":
    main()
