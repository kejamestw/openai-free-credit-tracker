from __future__ import annotations

from datetime import date
from typing import Any


SUPPORTED_CATALOG_SCHEMA = 1
KNOWN_GROUPS = frozenset({"standard", "mini"})
PRICE_FIELDS = frozenset({"input", "cached_input", "output"})


class CatalogValidationError(ValueError):
    """A stable, path-free catalog validation failure."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogValidationError(message)


def _iso_date(value: Any, field: str) -> date:
    _require(isinstance(value, str), f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogValidationError(f"{field} must be an ISO date") from exc


def validate_catalog(catalog: object, *, today: date | None = None) -> int:
    """Validate catalog schema v1 and return the identifier count.

    Unknown top-level and model fields are tolerated so schema v1 can gain optional
    metadata without breaking older readers. Required fields keep stable semantics.
    """

    _require(isinstance(catalog, dict), "catalog must be an object")
    data: dict[str, Any] = catalog
    _require(data.get("schema_version") == SUPPORTED_CATALOG_SCHEMA, "unsupported catalog schema")
    _require(isinstance(data.get("catalog_version"), str) and bool(data["catalog_version"].strip()), "catalog_version is required")
    effective_from = _iso_date(data.get("effective_from"), "effective_from")
    last_verified = _iso_date(data.get("last_verified"), "last_verified")
    _require(effective_from <= last_verified, "effective_from must not follow last_verified")
    _require(last_verified <= (today or date.today()), "last_verified must not be in the future")
    _require(data.get("currency") == "USD", "currency must be USD")
    _require(data.get("price_unit") == "per_1m_tokens", "unsupported price unit")

    sources = data.get("sources")
    _require(isinstance(sources, dict) and bool(sources), "sources are required")
    for source_id, source in sources.items():
        _require(isinstance(source_id, str) and bool(source_id), "source id is invalid")
        _require(isinstance(source, dict), f"source {source_id} must be an object")
        _require(source.get("kind") in {"first_party", "project_policy"}, f"source {source_id} kind is invalid")
        source_url = source.get("url")
        _require(source_url is None or (isinstance(source_url, str) and source_url.startswith("https://")), f"source {source_id} URL must use HTTPS")
        _iso_date(source.get("last_verified"), f"source {source_id} last_verified")

    groups = data.get("groups")
    _require(isinstance(groups, dict) and bool(groups), "groups are required")
    _require(set(groups).issubset(KNOWN_GROUPS), "catalog contains an unknown group")
    seen: set[str] = set()
    for group_id, group in groups.items():
        _require(isinstance(group, dict), f"group {group_id} must be an object")
        _require(isinstance(group.get("label"), str) and bool(group["label"].strip()), f"group {group_id} label is required")
        quota = group.get("daily_quota_tier_1_2")
        _require(isinstance(quota, int) and not isinstance(quota, bool) and quota > 0, f"group {group_id} quota is invalid")
        quota_source = group.get("quota_source")
        _require(isinstance(quota_source, str) and quota_source in sources, f"group {group_id} quota source is missing")
        models = group.get("models")
        _require(isinstance(models, list), f"group {group_id} models must be an array")
        for model in models:
            _require(isinstance(model, dict), f"group {group_id} model must be an object")
            model_id = model.get("id")
            aliases = model.get("aliases", [])
            _require(isinstance(model_id, str) and bool(model_id), "model id is required")
            _require(isinstance(aliases, list) and all(isinstance(alias, str) and bool(alias) for alias in aliases), f"model {model_id} aliases are invalid")
            identifiers = [model_id, *aliases]
            _require(not seen.intersection(identifiers), f"model {model_id} has a duplicate id or alias")
            seen.update(identifiers)
            _require(isinstance(model.get("enabled"), bool), f"model {model_id} enabled is required")
            _require(isinstance(model.get("eligible"), bool), f"model {model_id} eligible is required")
            _iso_date(model.get("effective_from"), f"model {model_id} effective_from")
            effective_until = model.get("effective_until")
            if effective_until is not None:
                _require(_iso_date(effective_until, f"model {model_id} effective_until") >= effective_from, f"model {model_id} effective range is invalid")
            pricing = model.get("pricing")
            _require(isinstance(pricing, dict) and set(pricing) == PRICE_FIELDS, f"model {model_id} pricing fields are invalid")
            _require(
                all(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 for value in pricing.values()),
                f"model {model_id} contains a negative or invalid price",
            )
            source_refs = model.get("source_refs")
            _require(isinstance(source_refs, dict), f"model {model_id} source_refs are required")
            _require(set(source_refs) == {"pricing", "eligibility"}, f"model {model_id} source_refs are invalid")
            _require(all(isinstance(ref, str) and ref in sources for ref in source_refs.values()), f"model {model_id} references an unknown source")
    return len(seen)
