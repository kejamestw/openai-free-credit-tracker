from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = ROOT / "build" / "pip-audit-cache"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the installed release environment.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or (ROOT / "build" / "dependency-audit.json")).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-c",
        (
            "import truststore; truststore.inject_into_ssl(); "
            "from pip_audit._cli import audit; audit()"
        ),
        "--local",
        "--skip-editable",
        "--progress-spinner",
        "off",
        "--cache-dir",
        str(DEFAULT_CACHE_DIR),
        "--format",
        "json",
        "--output",
        str(output),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    payload = json.loads(output.read_text(encoding="utf-8"))
    unexpected_skips = [
        dependency
        for dependency in payload.get("dependencies", [])
        if dependency.get("skip_reason")
        and dependency.get("name") != "openai-free-credit-tracker"
    ]
    if unexpected_skips:
        names = ", ".join(str(item.get("name", "unknown")) for item in unexpected_skips)
        raise RuntimeError(f"third-party dependencies could not be audited: {names}")
    print(f"Dependency audit generated: {output}")


if __name__ == "__main__":
    main()
