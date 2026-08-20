from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "OpenAI-Free-Credit-Tracker.cdx.json"


def _native_component(name: str, version: str, library: Path) -> dict:
    if not name.strip() or not version.strip():
        raise ValueError("native component name and version must not be empty")
    source = library.resolve(strict=True)
    if not source.is_file():
        raise ValueError("native component path must be a regular file")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "type": "library",
        "bom-ref": f"native:{name}:{version}:{source.name}:{digest}",
        "name": f"{name}:{source.name}",
        "version": version,
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {
                "name": "openai-free-credit-tracker:native-source-filename",
                "value": source.name,
            }
        ],
    }


def generate_sbom(
    output: Path,
    *,
    native_components: list[tuple[str, str, Path]] | None = None,
) -> Path:
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
        additions = [
            _native_component(name, version, library)
            for name, version, library in (native_components or [])
        ]
        payload["components"].extend(additions)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the release CycloneDX SBOM.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--native-component",
        nargs=3,
        action="append",
        default=[],
        metavar=("NAME", "VERSION", "LIBRARY"),
        help="record an exact native library version and SHA-256 in the platform SBOM",
    )
    args = parser.parse_args()
    path = generate_sbom(
        args.output,
        native_components=[
            (name, version, Path(library))
            for name, version, library in args.native_component
        ],
    )
    print(f"SBOM generated: {path}")


if __name__ == "__main__":
    main()
