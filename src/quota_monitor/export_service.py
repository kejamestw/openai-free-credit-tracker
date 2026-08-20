"""Versioned, privacy-aware exports of persisted usage history."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from .database import (
    DEFAULT_PROFILE_ID,
    DatabaseService,
    epoch_to_utc_text,
    mask_project_id,
    utc_now_text,
)


EXPORT_SCHEMA_VERSION = 1
PROJECT_ID_POLICIES = frozenset({"mask", "exclude", "include"})
ProjectIdPolicy = Literal["mask", "exclude", "include"]

CSV_COLUMNS = (
    "schema_version",
    "profile_id",
    "bucket_start_utc",
    "bucket_end_utc",
    "project_key",
    "project_name",
    "project_id",
    "model",
    "service_tier",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "total_tokens",
    "request_count",
    "catalog_version",
    "collected_at",
)


def csv_safe_text(value: str) -> str:
    """Neutralize text that spreadsheet applications may execute as a formula."""

    if not isinstance(value, str):
        raise TypeError("CSV text value must be a string")
    candidate = value.lstrip(" \t\r\n")
    if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _validate_policy(policy: str) -> ProjectIdPolicy:
    if policy not in PROJECT_ID_POLICIES:
        raise ValueError("project_id_policy must be mask, exclude, or include")
    return policy  # type: ignore[return-value]


def _project_id_for_export(project_id: str | None, policy: ProjectIdPolicy) -> str:
    if policy == "exclude" or project_id is None:
        return ""
    if policy == "mask":
        return mask_project_id(project_id)
    return project_id


def _export_record(row: dict, policy: ProjectIdPolicy) -> dict:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "profile_id": row["profile_id"],
        "bucket_start_utc": epoch_to_utc_text(row["bucket_start_utc"]),
        "bucket_end_utc": epoch_to_utc_text(row["bucket_end_utc"]),
        "project_key": row["project_key"],
        "project_name": row["project_name"],
        "project_id": _project_id_for_export(row.get("project_id"), policy),
        "model": row["model"],
        "service_tier": row["service_tier"],
        "input_tokens": row["input_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "output_tokens": row["output_tokens"],
        "total_tokens": row["total_tokens"],
        "request_count": row["request_count"],
        "catalog_version": row["catalog_version"],
        "collected_at": row["collected_at"],
    }


def build_export_records(
    database: DatabaseService,
    start_utc: int,
    end_utc: int,
    *,
    project_key: str | None = None,
    project_id_policy: ProjectIdPolicy = "mask",
    profile_id: str = DEFAULT_PROFILE_ID,
) -> list[dict]:
    policy = _validate_policy(project_id_policy)
    return [
        _export_record(row, policy)
        for row in database.query_usage(
            start_utc, end_utc, project_key=project_key, profile_id=profile_id
        )
    ]


def render_csv(records: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=CSV_COLUMNS,
        extrasaction="raise",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                column: csv_safe_text(value) if isinstance(value := record[column], str) else value
                for column in CSV_COLUMNS
            }
        )
    return stream.getvalue().encode("utf-8")


def render_json(
    records: list[dict],
    *,
    start_utc: int,
    end_utc: int,
    project_key: str | None,
    project_id_policy: ProjectIdPolicy,
    profile_id: str = DEFAULT_PROFILE_ID,
    generated_at: str | None = None,
) -> bytes:
    policy = _validate_policy(project_id_policy)
    envelope = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": generated_at or utc_now_text(),
        "filters": {
            "profile_id": profile_id,
            "start_utc": epoch_to_utc_text(start_utc),
            "end_utc": epoch_to_utc_text(end_utc),
            "project_key": project_key,
            "project_id_policy": policy,
        },
        "time_zone": "UTC",
        "records": records,
    }
    return (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path: str | Path, content: bytes) -> Path:
    """Publish a complete file or leave the previous destination untouched."""

    if not isinstance(content, bytes):
        raise TypeError("atomic_write content must be bytes")
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return destination
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def export_csv(
    database: DatabaseService,
    destination: str | Path,
    start_utc: int,
    end_utc: int,
    *,
    project_key: str | None = None,
    project_id_policy: ProjectIdPolicy = "mask",
    profile_id: str = DEFAULT_PROFILE_ID,
) -> Path:
    records = build_export_records(
        database,
        start_utc,
        end_utc,
        project_key=project_key,
        project_id_policy=project_id_policy,
        profile_id=profile_id,
    )
    return atomic_write(destination, render_csv(records))


def export_json(
    database: DatabaseService,
    destination: str | Path,
    start_utc: int,
    end_utc: int,
    *,
    project_key: str | None = None,
    project_id_policy: ProjectIdPolicy = "mask",
    generated_at: str | None = None,
    profile_id: str = DEFAULT_PROFILE_ID,
) -> Path:
    records = build_export_records(
        database,
        start_utc,
        end_utc,
        project_key=project_key,
        project_id_policy=project_id_policy,
        profile_id=profile_id,
    )
    content = render_json(
        records,
        start_utc=start_utc,
        end_utc=end_utc,
        project_key=project_key,
        project_id_policy=project_id_policy,
        profile_id=profile_id,
        generated_at=generated_at,
    )
    return atomic_write(destination, content)
