from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Protocol


UNATTRIBUTED_PROJECT_KEY = "unattributed"


class UpstreamContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UsageBucketRecord:
    bucket_start_utc: int
    bucket_end_utc: int
    project_key: str
    model: str
    service_tier: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    request_count: int
    project_id_private: str | None = field(repr=False)


@dataclass(frozen=True)
class UsageSlice:
    requested_start_utc: int
    requested_end_utc: int
    records: tuple[UsageBucketRecord, ...]
    pages_fetched: int


class AdminUsageClient(Protocol):
    def get(self, path: str, params: dict) -> dict: ...


class ProjectKeyDeriver:
    """Create a stable local pseudonym without persisting the upstream project ID."""

    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("project key secret must be at least 16 bytes")
        self._secret = bytes(secret)

    def derive(self, project_id: object) -> str:
        if project_id is None or project_id == "":
            return UNATTRIBUTED_PROJECT_KEY
        if not isinstance(project_id, str):
            raise UpstreamContractError("usage_project_invalid", "Usage project identifier is invalid.")
        digest = hmac.new(self._secret, project_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"project-{digest[:24]}"


def _nonnegative_int(row: dict, field: str) -> int:
    value = row.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpstreamContractError("usage_field_invalid", f"Usage {field} is invalid.")
    return value


def _page(payload: object) -> tuple[list, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise UpstreamContractError("usage_response_invalid", "Usage response does not contain a data list.")
    cursor = payload.get("next_page")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise UpstreamContractError("usage_cursor_invalid", "Usage pagination cursor is invalid.")
    if payload.get("has_more") is True and cursor is None:
        raise UpstreamContractError("usage_cursor_missing", "Usage pagination cursor is missing.")
    return payload["data"], cursor


def parse_usage_buckets(
    buckets: object,
    *,
    project_keys: ProjectKeyDeriver,
    requested_start_utc: int,
    requested_end_utc: int,
) -> list[UsageBucketRecord]:
    if not isinstance(buckets, list):
        raise UpstreamContractError("usage_response_invalid", "Usage buckets must be a list.")
    parsed: list[UsageBucketRecord] = []
    for bucket in buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise UpstreamContractError("usage_bucket_invalid", "Usage bucket is invalid.")
        start = bucket.get("start_time", requested_start_utc)
        end = bucket.get("end_time", requested_end_utc)
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < requested_start_utc
            or end > requested_end_utc
            or end <= start
        ):
            raise UpstreamContractError("usage_bucket_time_invalid", "Usage bucket time range is invalid.")
        for row in bucket["results"]:
            if not isinstance(row, dict):
                raise UpstreamContractError("usage_result_invalid", "Usage result is invalid.")
            model_value = row.get("model")
            if model_value is None or model_value == "":
                model = "unknown"
            elif isinstance(model_value, str):
                model = model_value
            else:
                raise UpstreamContractError("usage_model_invalid", "Usage model is invalid.")
            service_tier_value = row.get("service_tier")
            if service_tier_value is None or service_tier_value == "":
                service_tier = "unknown"
            elif isinstance(service_tier_value, str):
                service_tier = service_tier_value
            else:
                raise UpstreamContractError("usage_service_tier_invalid", "Usage service tier is invalid.")
            input_tokens = _nonnegative_int(row, "input_tokens")
            cached_tokens = _nonnegative_int(row, "input_cached_tokens")
            cache_write_tokens = _nonnegative_int(row, "input_cache_write_tokens")
            output_tokens = _nonnegative_int(row, "output_tokens")
            request_count = _nonnegative_int(row, "num_model_requests")
            if cached_tokens > input_tokens:
                raise UpstreamContractError("usage_cached_tokens_invalid", "Cached input exceeds total input.")
            if cache_write_tokens > input_tokens:
                raise UpstreamContractError(
                    "usage_cache_write_tokens_invalid", "Cache-write input exceeds total input."
                )
            parsed.append(
                UsageBucketRecord(
                    bucket_start_utc=start,
                    bucket_end_utc=end,
                    project_key=project_keys.derive(row.get("project_id")),
                    model=model,
                    service_tier=service_tier,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_tokens,
                    output_tokens=output_tokens,
                    request_count=request_count,
                    project_id_private=row.get("project_id") or None,
                )
            )
    return parsed


def fetch_usage_slice(
    client: AdminUsageClient,
    *,
    start_time: int,
    end_time: int,
    project_keys: ProjectKeyDeriver,
    bucket_width: str = "1d",
) -> UsageSlice:
    if start_time < 0 or end_time <= start_time:
        raise ValueError("usage slice must have a positive time range")
    base_params = {
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": bucket_width,
        "limit": 31 if bucket_width == "1d" else 168,
        "group_by": ["model", "project_id", "service_tier"],
    }
    cursor = None
    seen: set[str] = set()
    records: list[UsageBucketRecord] = []
    pages = 0
    while True:
        params = dict(base_params)
        if cursor is not None:
            params["page"] = cursor
        payload = client.get("/organization/usage/completions", params)
        buckets, next_cursor = _page(payload)
        pages += 1
        records.extend(
            parse_usage_buckets(
                buckets,
                project_keys=project_keys,
                requested_start_utc=start_time,
                requested_end_utc=end_time,
            )
        )
        if next_cursor is None:
            break
        if next_cursor in seen:
            raise UpstreamContractError("usage_cursor_repeated", "Usage pagination cursor was repeated.")
        seen.add(next_cursor)
        cursor = next_cursor
    return UsageSlice(start_time, end_time, tuple(records), pages)
