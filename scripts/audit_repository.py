"""Fail release checks on leaked key-shaped strings or corrupt tracked text."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(rb"sk-(?:admin|proj)-[A-Za-z0-9_-]{10,}")
ALLOWED_CONTROLS = {9, 10, 13}


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def tracked_text_files() -> list[tuple[Path, bytes]]:
    files: list[tuple[Path, bytes]] = []
    for encoded_path in git_bytes("ls-files", "-z").split(b"\0"):
        if not encoded_path:
            continue
        path = ROOT / encoded_path.decode("utf-8")
        if not path.is_file():
            continue
        content = path.read_bytes()
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files.append((path, content))
    return files


def audit_tracked_text(files: list[tuple[Path, bytes]]) -> list[str]:
    findings: list[str] = []
    for path, content in files:
        relative = path.relative_to(ROOT)
        if SECRET_PATTERN.search(content):
            findings.append(f"potential API key in tracked file: {relative}")
        for offset, value in enumerate(content):
            if (value < 32 and value not in ALLOWED_CONTROLS) or value == 127:
                findings.append(
                    f"unexpected control character: {relative}:{offset}:0x{value:02x}"
                )
    return findings


def audit_git_history() -> list[str]:
    history = git_bytes("log", "-p", "--all", "--no-ext-diff", "--no-textconv", "--", ".")
    if SECRET_PATTERN.search(history):
        return ["potential API key found in Git patch history"]
    return []


def main() -> None:
    files = tracked_text_files()
    findings = [*audit_tracked_text(files), *audit_git_history()]
    if findings:
        raise SystemExit("repository audit failed:\n- " + "\n- ".join(findings))
    print(f"repository audit passed: {len(files)} tracked UTF-8 text files; history scanned")


if __name__ == "__main__":
    main()
