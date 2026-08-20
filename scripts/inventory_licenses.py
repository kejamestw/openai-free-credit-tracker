from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


UNKNOWN_MARKERS = frozenset({"", "unknown", "n/a", "none"})


def _license_values(item: metadata.PackageMetadata) -> list[str]:
    values: list[str] = []
    expression = item.get("License-Expression", "").strip()
    legacy = item.get("License", "").strip()
    if expression.lower() not in UNKNOWN_MARKERS:
        values.append(expression)
    elif legacy.lower() not in UNKNOWN_MARKERS:
        values.append(legacy)
    values.extend(
        classifier.removeprefix("License :: ").strip()
        for classifier in item.get_all("Classifier", [])
        if classifier.startswith("License :: ")
    )
    return sorted(set(value for value in values if value))


def build_inventory(distributions=None) -> dict:
    installed = distributions if distributions is not None else metadata.distributions()
    packages: dict[str, dict] = {}
    for distribution in installed:
        name = distribution.metadata.get("Name", "").strip()
        if not name:
            continue
        normalized = name.lower().replace("_", "-")
        packages[normalized] = {
            "name": name,
            "version": distribution.version,
            "licenses": _license_values(distribution.metadata),
        }
    ordered = [packages[name] for name in sorted(packages)]
    unknown = [item["name"] for item in ordered if not item["licenses"]]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packages": ordered,
        "summary": {
            "package_count": len(ordered),
            "unknown_license_count": len(unknown),
            "unknown_license_packages": unknown,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory installed Python package licenses.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-on-unknown", action="store_true")
    args = parser.parse_args()
    payload = build_inventory()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"License inventory generated: {output}")
    if args.fail_on_unknown and payload["summary"]["unknown_license_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
