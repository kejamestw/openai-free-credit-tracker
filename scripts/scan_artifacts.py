from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


class MalwareScanError(RuntimeError):
    pass


class MalwareDetectedError(MalwareScanError):
    pass


@dataclass(frozen=True)
class Scanner:
    kind: str
    executable: Path


def _windows_defender_candidates() -> list[Path]:
    candidates: list[Path] = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        platform_root = Path(program_data) / "Microsoft" / "Windows Defender" / "Platform"
        if platform_root.is_dir():
            candidates.extend(
                sorted(platform_root.glob("*/MpCmdRun.exe"), reverse=True)
            )
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Windows Defender" / "MpCmdRun.exe")
    return candidates


def discover_scanner(kind: str) -> Scanner:
    if kind not in {"auto", "clamscan", "defender"}:
        raise ValueError("scanner must be auto, clamscan, or defender")
    if kind in {"auto", "clamscan"}:
        executable = shutil.which("clamscan")
        if executable:
            return Scanner("clamscan", Path(executable).resolve())
        if kind == "clamscan":
            raise MalwareScanError("clamscan is required but was not found")
    if kind in {"auto", "defender"}:
        command = shutil.which("MpCmdRun.exe")
        candidates = ([Path(command)] if command else []) + _windows_defender_candidates()
        for candidate in candidates:
            if candidate.is_file():
                return Scanner("defender", candidate.resolve())
        if kind == "defender":
            raise MalwareScanError("Microsoft Defender command-line scanner was not found")
    raise MalwareScanError("no supported malware scanner was found")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command(scanner: Scanner, path: Path) -> list[str]:
    if scanner.kind == "clamscan":
        return [str(scanner.executable), "--no-summary", "--infected", "--", str(path)]
    if scanner.kind == "defender":
        return [
            str(scanner.executable),
            "-Scan",
            "-ScanType",
            "3",
            "-File",
            str(path),
            "-DisableRemediation",
            "-ReturnHR",
        ]
    raise ValueError("unsupported scanner")


def _scanner_version(
    scanner: Scanner,
    runner: Callable[..., subprocess.CompletedProcess],
) -> str:
    command = (
        [str(scanner.executable), "--version"]
        if scanner.kind == "clamscan"
        else [str(scanner.executable), "-?"]
    )
    completed = runner(
        command,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise MalwareScanError("malware scanner version probe failed")
    output = completed.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    line = str(output).strip().splitlines()[0] if str(output).strip() else ""
    if not line or len(line) > 512:
        raise MalwareScanError("malware scanner version is unavailable")
    if scanner.kind == "clamscan" and (not line.startswith("ClamAV ") or "/" not in line):
        raise MalwareScanError("ClamAV did not report a loaded signature database")
    return line


def scan_artifacts(
    paths: Sequence[Path],
    scanner: Scanner,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    if not paths:
        raise ValueError("at least one artifact is required")
    scanner_version = _scanner_version(scanner, runner)
    results = []
    for supplied in paths:
        if supplied.is_symlink():
            raise MalwareScanError("artifact symlinks are not accepted")
        path = supplied.resolve(strict=True)
        if not path.is_file():
            raise MalwareScanError("artifact must be a regular file")
        completed = runner(
            _command(scanner, path),
            capture_output=True,
            timeout=600,
            check=False,
        )
        if completed.returncode == 1:
            raise MalwareDetectedError(f"malware scanner flagged artifact: {path.name}")
        if completed.returncode != 0:
            raise MalwareScanError(
                f"malware scanner failed for {path.name} with exit code {completed.returncode}"
            )
        results.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "status": "clean",
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanner": scanner.kind,
        "scanner_version": scanner_version,
        "passed": True,
        "artifacts": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-closed malware scan for release artifacts.")
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--scanner", choices=("auto", "clamscan", "defender"), default="auto")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scanner = discover_scanner(args.scanner)
    payload = scan_artifacts(args.artifacts, scanner)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Malware scan passed: {output}")


if __name__ == "__main__":
    main()
