import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .catalog_schema import CatalogValidationError, validate_catalog

DATE_SUFFIX = re.compile(r"-20\d{2}-\d{2}-\d{2}$")


def resource_root() -> Path:
    """Return the source root or PyInstaller's one-file extraction root."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


@dataclass(frozen=True)
class CatalogLoadResult:
    catalog: dict
    used_fallback: bool = False
    warning_code: str | None = None


def _read_catalog(path: Path) -> dict:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("catalog could not be read") from exc
    validate_catalog(catalog)
    return catalog


def load_catalog_status(path: Path | None = None) -> CatalogLoadResult:
    """Load a catalog and safely fall back to the bundled last-known-good copy."""

    bundled_path = resource_path("data", "models.json")
    catalog_path = path or bundled_path
    try:
        return CatalogLoadResult(_read_catalog(catalog_path))
    except CatalogValidationError:
        if path is None or catalog_path.resolve() == bundled_path.resolve():
            raise
        return CatalogLoadResult(
            _read_catalog(bundled_path),
            used_fallback=True,
            warning_code="catalog_fallback",
        )


def load_catalog(path: Path | None = None) -> dict:
    return load_catalog_status(path).catalog


def clean_model_name(name: str) -> str:
    return DATE_SUFFIX.sub("", name or "unknown")


def build_index(catalog: dict) -> dict:
    index = {}
    for group_id, group in catalog["groups"].items():
        for model in group["models"]:
            entry = {**model, "group": group_id}
            index[model["id"]] = entry
            for alias in model.get("aliases", []):
                index[alias] = entry
    return index


def find_model(name: str, catalog: dict) -> dict | None:
    index = build_index(catalog)
    return index.get(name) or index.get(clean_model_name(name))
