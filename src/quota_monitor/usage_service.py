from datetime import datetime, timezone

from .classification import estimate_cost, is_incentivized
from .model_catalog import clean_model_name, find_model


class UsageDataError(ValueError):
    """Raised when an upstream Usage API response cannot be safely interpreted."""


def utc_day_range(now: datetime | None = None) -> tuple[int, int]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    current_utc = current.astimezone(timezone.utc)
    start = current_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(current_utc.timestamp())


def _usage_page(payload: object) -> tuple[list, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise UsageDataError("Usage API response must contain a data list")
    next_page = payload.get("next_page")
    if next_page is not None and (not isinstance(next_page, str) or not next_page):
        raise UsageDataError("Usage API response contains an invalid pagination cursor")
    if payload.get("has_more") is True and next_page is None:
        raise UsageDataError("Usage API response is missing its next pagination cursor")
    return payload["data"], next_page


def fetch_usage(client, catalog: dict, now: datetime | None = None) -> dict:
    start, end = utc_day_range(now)
    if end <= start:
        return summarize_usage([], catalog, start, end)
    base_params = {
        "start_time": start,
        "end_time": end,
        "bucket_width": "1h",
        "limit": 24,
        "group_by": ["model", "project_id", "service_tier"],
    }
    buckets = []
    page = None
    seen_pages = set()
    while True:
        params = base_params.copy()
        if page is not None:
            params["page"] = page
        payload = client.get("/organization/usage/completions", params)
        page_buckets, next_page = _usage_page(payload)
        buckets.extend(page_buckets)
        if next_page is None:
            break
        if next_page in seen_pages:
            raise UsageDataError("Usage API returned a repeated pagination cursor")
        seen_pages.add(next_page)
        page = next_page
    return summarize_usage(buckets, catalog, start, end)


def _token_count(row: dict, field: str) -> int:
    value = row.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageDataError(f"Usage API response has invalid {field}")
    return value


def _empty_totals() -> dict:
    return {"input": 0, "cached_input": 0, "output": 0, "total": 0}


def _add_tokens(target: dict, input_tokens: int, cached_tokens: int, output_tokens: int) -> None:
    target["input"] += input_tokens
    target["cached_input"] += cached_tokens
    target["output"] += output_tokens
    target["total"] += input_tokens + output_tokens


def summarize_usage(buckets: list, catalog: dict, start: int = 0, end: int = 0) -> dict:
    if not isinstance(buckets, list):
        raise UsageDataError("Usage API response data must be a list")
    groups = {
        group_id: {**_empty_totals(), "models": {}}
        for group_id in catalog["groups"]
    }
    other_usage = _empty_totals()
    list_price_estimate = 0.0
    unpriced_tokens = 0
    debug = []

    for bucket in buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise UsageDataError("Usage API response bucket must contain a results list")
        for row in bucket["results"]:
            if not isinstance(row, dict):
                raise UsageDataError("Usage API response result must be an object")
            raw_name = row.get("model") or "unknown"
            if not isinstance(raw_name, str):
                raise UsageDataError("Usage API response has invalid model")
            service_tier = row.get("service_tier")
            if service_tier is not None and not isinstance(service_tier, str):
                raise UsageDataError("Usage API response has invalid service_tier")

            display_name = clean_model_name(raw_name)
            entry = find_model(raw_name, catalog)
            input_tokens = _token_count(row, "input_tokens")
            cached_tokens = _token_count(row, "input_cached_tokens")
            output_tokens = _token_count(row, "output_tokens")
            if cached_tokens > input_tokens:
                raise UsageDataError("Usage API response has input_cached_tokens greater than input_tokens")
            total = input_tokens + output_tokens
            free = is_incentivized(service_tier)
            eligible_entry = entry if entry and entry.get("eligible", True) else None

            if entry:
                list_price_estimate += estimate_cost(
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    entry["pricing"],
                )
            else:
                unpriced_tokens += total

            if free and eligible_entry:
                group = groups[eligible_entry["group"]]
                _add_tokens(group, input_tokens, cached_tokens, output_tokens)
                model = group["models"].setdefault(display_name, _empty_totals())
                _add_tokens(model, input_tokens, cached_tokens, output_tokens)
                category = "complimentary"
            else:
                _add_tokens(other_usage, input_tokens, cached_tokens, output_tokens)
                category = "other"

            debug.append(
                {
                    "model": display_name,
                    "service_tier": service_tier,
                    "tokens": total,
                    "category": category,
                }
            )

    return {
        "groups": groups,
        "other_usage": other_usage,
        "other_tokens": other_usage["total"],
        "list_price_estimate_usd": list_price_estimate,
        "list_price": list_price_estimate,
        "unpriced_tokens": unpriced_tokens,
        "start": start,
        "end": end,
        "debug": debug,
    }
