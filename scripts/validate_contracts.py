from __future__ import annotations

from pathlib import Path

from quota_monitor.catalog_schema import validate_catalog
from quota_monitor.config_service import (
    UnknownFieldPolicy,
    assert_no_sensitive_data,
    config_to_dict,
    default_config,
    parse_config,
)
from quota_monitor.database import SCHEMA_VERSION
from quota_monitor.export_service import CSV_COLUMNS, EXPORT_SCHEMA_VERSION
from quota_monitor.i18n import load_locale_directory
from quota_monitor.model_catalog import load_catalog
from quota_monitor.semver import SemVer
from quota_monitor.update_manifest import UPDATE_MANIFEST_SCHEMA_VERSION
from quota_monitor.version import __version__


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FILES = (
    "README.md",
    "api-v1.md",
    "config-v1.md",
    "database-v1.md",
    "export-v1.md",
    "catalog-v1.md",
    "update-manifest-v1.md",
    "compatibility.md",
)


def validate_contracts() -> dict[str, int | str]:
    contract_root = ROOT / "docs" / "contracts"
    for name in CONTRACT_FILES:
        path = contract_root / name
        if not path.is_file() or path.stat().st_size < 80:
            raise RuntimeError(f"contract file is missing or empty: {name}")

    parsed_version = SemVer.parse(__version__)
    default_document = config_to_dict(default_config())
    assert_no_sensitive_data(default_document)
    if parse_config(default_document, unknown_fields=UnknownFieldPolicy.PRESERVE) != default_config():
        raise RuntimeError("default config does not round-trip")

    catalog_identifiers = validate_catalog(load_catalog())
    if EXPORT_SCHEMA_VERSION != 1 or "schema_version" not in CSV_COLUMNS:
        raise RuntimeError("export schema v1 is not stable")
    if UPDATE_MANIFEST_SCHEMA_VERSION != 1:
        raise RuntimeError("update manifest schema v1 is not stable")
    if SCHEMA_VERSION < 1:
        raise RuntimeError("database schema version is invalid")
    locales = load_locale_directory(ROOT / "locales")

    server_source = (ROOT / "src" / "quota_monitor" / "server.py").read_text(encoding="utf-8")
    for endpoint in ("/api/v1/health", "/api/v1/catalog", "/api/v1/data"):
        if endpoint not in server_source:
            raise RuntimeError(f"public endpoint is not implemented: {endpoint}")

    return {
        "app_version": str(parsed_version),
        "database_schema": SCHEMA_VERSION,
        "export_schema": EXPORT_SCHEMA_VERSION,
        "catalog_identifiers": catalog_identifiers,
        "locales": len(locales),
        "contract_files": len(CONTRACT_FILES),
    }


def main() -> None:
    result = validate_contracts()
    details = ", ".join(f"{key}={value}" for key, value in result.items())
    print(f"contracts valid: {details}")


if __name__ == "__main__":
    main()
