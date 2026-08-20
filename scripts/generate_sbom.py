from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "OpenAI-Free-Credit-Tracker.cdx.json"


def generate_sbom(output: Path) -> Path:
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "environment",
                "--output-format",
                "JSON",
                "--spec-version",
                "1.6",
                "--output-file",
                str(temporary),
            ],
            cwd=ROOT,
            check=True,
        )
        payload = json.loads(temporary.read_text(encoding="utf-8"))
        if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
            raise RuntimeError("SBOM generator returned an unexpected document")
        if not isinstance(payload.get("components"), list):
            raise RuntimeError("SBOM does not contain a components list")
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the release CycloneDX SBOM.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    path = generate_sbom(args.output)
    print(f"SBOM generated: {path}")


if __name__ == "__main__":
    main()
