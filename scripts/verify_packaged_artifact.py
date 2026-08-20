"""Black-box checks for a native packaged application executable."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


def command_for(executable: Path, arguments: list[str], *, appimage: bool = False) -> list[str]:
    command = [str(executable.resolve())]
    if appimage:
        command.append("--appimage-extract-and-run")
    return command + arguments


def _tree_snapshot(root: Path) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


def verify(
    executable: Path,
    expected_version: str,
    *,
    appimage: bool = False,
    expect_update_trust: bool = False,
) -> None:
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"packaged executable was not found: {executable}")
    with tempfile.TemporaryDirectory(prefix="quota-monitor-verify-") as temporary:
        root = Path(temporary).resolve()
        environment = os.environ.copy()
        for kind in ("CONFIG", "DATA", "CACHE", "LOG"):
            environment[f"OPENAI_CREDIT_TRACKER_{kind}_DIR"] = str(root / kind.lower())

        version = subprocess.run(
            command_for(executable, ["--version"], appimage=appimage),
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not version.stdout.strip().endswith(f" {expected_version}"):
            raise RuntimeError(f"unexpected packaged version: {version.stdout.strip()}")
        before_import = _tree_snapshot(root)
        import_smoke = subprocess.run(
            command_for(executable, ["--packaged-import-smoke"], appimage=appimage),
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        expected_state = "present" if expect_update_trust else "absent"
        if f"update trust {expected_state}" not in import_smoke.stdout:
            raise RuntimeError(
                f"packaged update trust state is not {expected_state}: {import_smoke.stdout.strip()}"
            )
        if _tree_snapshot(root) != before_import:
            raise RuntimeError("packaged import smoke wrote application state")
        for flag in ("--smoke-test", "--packaged-self-test"):
            subprocess.run(
                command_for(executable, [flag], appimage=appimage),
                cwd=root,
                env=environment,
                check=True,
                timeout=60,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--appimage", action="store_true")
    parser.add_argument("--expect-update-trust", action="store_true")
    args = parser.parse_args()
    verify(
        args.executable,
        args.expected_version,
        appimage=args.appimage,
        expect_update_trust=args.expect_update_trust,
    )
    print(f"Packaged artifact verified: {args.executable}")


if __name__ == "__main__":
    main()
