class CostsDataError(ValueError):
    """Raised when an upstream Costs API response cannot be safely interpreted."""


def _costs_page(payload: object) -> tuple[list, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CostsDataError("Costs API response must contain a data list")
    next_page = payload.get("next_page")
    if next_page is not None and (not isinstance(next_page, str) or not next_page):
        raise CostsDataError("Costs API response contains an invalid pagination cursor")
    if payload.get("has_more") is True and next_page is None:
        raise CostsDataError("Costs API response is missing its next pagination cursor")
    return payload["data"], next_page


def _page_total(buckets: list) -> float:
    total = 0.0
    for bucket in buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise CostsDataError("Costs API response bucket must contain a results list")
        for row in bucket["results"]:
            if not isinstance(row, dict) or not isinstance(row.get("amount"), dict):
                raise CostsDataError("Costs API response result must contain an amount")
            amount = row["amount"]
            value = amount.get("value")
            currency = amount.get("currency")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CostsDataError("Costs API response amount must be numeric")
            if not isinstance(currency, str) or currency.lower() != "usd":
                raise CostsDataError("Costs API response currency must be usd")
            total += float(value)
    return total


def fetch_costs(client, start_time: int, end_time: int) -> dict:
    if end_time <= start_time:
        return {"actual_usd": 0.0, "available": True, "error": None}
    base_params = {
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": "1d",
        "limit": 1,
    }
    total = 0.0
    page = None
    seen_pages = set()
    try:
        while True:
            params = base_params.copy()
            if page is not None:
                params["page"] = page
            payload = client.get("/organization/costs", params)
            buckets, next_page = _costs_page(payload)
            total += _page_total(buckets)
            if next_page is None:
                break
            if next_page in seen_pages:
                raise CostsDataError("Costs API returned a repeated pagination cursor")
            seen_pages.add(next_page)
            page = next_page
    except CostsDataError:
        return {
            "actual_usd": None,
            "available": False,
            "error": {
                "code": "costs_response_invalid",
                "message": "Costs API returned data in an unsupported format.",
            },
        }
    except Exception:
        return {
            "actual_usd": None,
            "available": False,
            "error": {
                "code": "costs_unavailable",
                "message": "Costs API data is temporarily unavailable.",
            },
        }
    return {"actual_usd": total, "available": True, "error": None}
