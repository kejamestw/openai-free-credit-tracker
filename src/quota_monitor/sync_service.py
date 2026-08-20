"""Historical usage synchronization and explicit history maintenance operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .database import (
    DEFAULT_PROFILE_ID,
    DatabaseError,
    DatabaseService,
    IntegrityResult,
    UsageBucket,
    utc_now_text,
)
from .openai_client import OpenAIClientError
from .upstream_adapter import (
    AdminUsageClient,
    ProjectKeyDeriver,
    UpstreamContractError,
    fetch_usage_slice,
)


SECONDS_PER_DAY = 86_400
DEFAULT_SYNC_DAYS = 30
MAX_SYNC_DAYS = 366
_SYNC_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class SyncProgress:
    event: str
    run_id: str
    completed_slices: int
    total_slices: int
    slice_start_utc: int | None = None
    slice_end_utc: int | None = None
    pages_fetched: int = 0
    error_code: str | None = None
    resumed: bool = False
    profile_id: str = DEFAULT_PROFILE_ID


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    requested_start_utc: int
    requested_end_utc: int
    status: str
    completed_slices: int
    total_slices: int
    pages_fetched: int
    error_code: str | None
    resumed: bool
    profile_id: str = DEFAULT_PROFILE_ID


@dataclass(frozen=True)
class RetentionPreview:
    database_path: str
    profile_id: str
    retention_days: int
    cutoff_utc: int
    row_count: int
    oldest_bucket_start_utc: int | None
    newest_bucket_end_utc: int | None


@dataclass(frozen=True)
class RetentionResult:
    cutoff_utc: int
    deleted_rows: int


ProgressCallback = Callable[[SyncProgress], None]


def default_sync_range(
    now: datetime | None = None, *, days: int = DEFAULT_SYNC_DAYS
) -> tuple[int, int]:
    """Return the most recent complete UTC days, stable across same-day restarts."""

    _validate_days(days, maximum=MAX_SYNC_DAYS)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    midnight = current.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = int(midnight.timestamp())
    return end - days * SECONDS_PER_DAY, end


def utc_day_slices(start_utc: int, end_utc: int) -> tuple[tuple[int, int], ...]:
    """Split any positive range at fixed 00:00:00Z boundaries."""

    _validate_sync_range(start_utc, end_utc)
    slices = []
    cursor = start_utc
    while cursor < end_utc:
        next_midnight = cursor - cursor % SECONDS_PER_DAY + SECONDS_PER_DAY
        slice_end = min(next_midnight, end_utc)
        slices.append((cursor, slice_end))
        cursor = slice_end
    return tuple(slices)


class _CheckpointingClient:
    """Observe successful pages without exposing response contents to progress events."""

    def __init__(
        self,
        client: AdminUsageClient,
        database: DatabaseService,
        run_id: str,
        slice_start_utc: int,
        slice_end_utc: int,
        emit_page: Callable[[int], None],
        profile_id: str,
    ):
        self.client = client
        self.database = database
        self.run_id = run_id
        self.slice_start_utc = slice_start_utc
        self.slice_end_utc = slice_end_utc
        self.emit_page = emit_page
        self.profile_id = profile_id
        self.pages_fetched = 0
        self.checkpoint: str | None = None

    def get(self, path: str, params: dict) -> dict:
        payload = self.client.get(path, params)
        self.pages_fetched += 1
        cursor = payload.get("next_page") if isinstance(payload, dict) else None
        self.checkpoint = cursor if isinstance(cursor, str) and cursor else None
        self.database.record_slice_checkpoint(
            self.run_id,
            self.slice_start_utc,
            self.slice_end_utc,
            checkpoint=self.checkpoint,
            pages_fetched=self.pages_fetched,
            status="in_progress",
            profile_id=self.profile_id,
        )
        self.emit_page(self.pages_fetched)
        return payload


class UsageSyncService:
    def __init__(
        self,
        database: DatabaseService,
        *,
        project_keys: ProjectKeyDeriver,
        catalog_version: str,
        profile_id: str = DEFAULT_PROFILE_ID,
    ):
        if not isinstance(catalog_version, str) or not catalog_version.strip():
            raise ValueError("catalog_version must be a non-empty string")
        self.database = database
        self.project_keys = project_keys
        self.catalog_version = catalog_version.strip()
        self.profile_id = profile_id

    def sync(
        self,
        client: AdminUsageClient,
        *,
        start_utc: int | None = None,
        end_utc: int | None = None,
        now: datetime | None = None,
        days: int = DEFAULT_SYNC_DAYS,
        resume: bool = True,
        progress: ProgressCallback | None = None,
    ) -> SyncResult:
        if (start_utc is None) != (end_utc is None):
            raise ValueError("start_utc and end_utc must be provided together")
        if start_utc is None:
            start_utc, end_utc = default_sync_range(now, days=days)
        assert end_utc is not None
        _validate_sync_range(start_utc, end_utc)
        slices = utc_day_slices(start_utc, end_utc)

        resumable = (
            self.database.find_resumable_collection_run(
                start_utc, end_utc, profile_id=self.profile_id
            )
            if resume
            else None
        )
        resumed = resumable is not None
        if resumable:
            run_id = resumable["run_id"]
            self.database.resume_collection_run(run_id, profile_id=self.profile_id)
        else:
            run_id = self.database.start_collection_run(
                start_utc, end_utc, profile_id=self.profile_id
            )

        prior = {
            (item["slice_start_utc"], item["slice_end_utc"]): item
            for item in self.database.list_collection_slices(
                run_id, profile_id=self.profile_id
            )
        }
        completed = sum(
            1 for item in prior.values() if item["status"] == "completed"
        )
        self._emit(
            progress,
            SyncProgress(
                "run_started", run_id, completed, len(slices),
                resumed=resumed, profile_id=self.profile_id
            ),
        )

        for slice_start, slice_end in slices:
            existing = prior.get((slice_start, slice_end))
            if existing and existing["status"] == "completed":
                self._emit(
                    progress,
                    SyncProgress(
                        "slice_skipped",
                        run_id,
                        completed,
                        len(slices),
                        slice_start,
                        slice_end,
                        existing["pages_fetched"],
                        resumed=resumed,
                        profile_id=self.profile_id,
                    ),
                )
                continue

            self.database.record_slice_checkpoint(
                run_id,
                slice_start,
                slice_end,
                checkpoint=None,
                pages_fetched=0,
                status="in_progress",
                profile_id=self.profile_id,
            )
            self._emit(
                progress,
                SyncProgress(
                    "slice_started",
                    run_id,
                    completed,
                    len(slices),
                    slice_start,
                    slice_end,
                    resumed=resumed,
                    profile_id=self.profile_id,
                ),
            )

            observer = _CheckpointingClient(
                client,
                self.database,
                run_id,
                slice_start,
                slice_end,
                lambda pages, start=slice_start, end=slice_end: self._emit(
                    progress,
                    SyncProgress(
                        "page_fetched",
                        run_id,
                        completed,
                        len(slices),
                        start,
                        end,
                        pages,
                        resumed=resumed,
                        profile_id=self.profile_id,
                    ),
                ),
                self.profile_id,
            )
            try:
                fetched = fetch_usage_slice(
                    observer,
                    start_time=slice_start,
                    end_time=slice_end,
                    project_keys=self.project_keys,
                    bucket_width="1d",
                )
                collected_at = utc_now_text(now)
                records = tuple(self._to_database_bucket(item, collected_at) for item in fetched.records)
                self.database.reconcile_slice(
                    run_id,
                    slice_start,
                    slice_end,
                    records,
                    pages_fetched=fetched.pages_fetched,
                    profile_id=self.profile_id,
                )
            except DatabaseError:
                raise
            except Exception as exc:
                error_code = _safe_sync_error_code(exc)
                self.database.record_slice_checkpoint(
                    run_id,
                    slice_start,
                    slice_end,
                    checkpoint=observer.checkpoint,
                    pages_fetched=observer.pages_fetched,
                    status="failed",
                    error_code=error_code,
                    profile_id=self.profile_id,
                )
                status = "partial" if completed else "failed"
                self.database.finish_collection_run(
                    run_id, status, error_code=error_code, profile_id=self.profile_id
                )
                self._emit(
                    progress,
                    SyncProgress(
                        "slice_failed",
                        run_id,
                        completed,
                        len(slices),
                        slice_start,
                        slice_end,
                        observer.pages_fetched,
                        error_code,
                        resumed,
                        self.profile_id,
                    ),
                )
                run = self.database.get_collection_run(run_id, profile_id=self.profile_id)
                return SyncResult(
                    run_id,
                    start_utc,
                    end_utc,
                    status,
                    completed,
                    len(slices),
                    run["pages_fetched"],
                    error_code,
                    resumed,
                    self.profile_id,
                )

            completed += 1
            self._emit(
                progress,
                SyncProgress(
                    "slice_completed",
                    run_id,
                    completed,
                    len(slices),
                    slice_start,
                    slice_end,
                    fetched.pages_fetched,
                    resumed=resumed,
                    profile_id=self.profile_id,
                ),
            )

        self.database.finish_collection_run(
            run_id, "completed", profile_id=self.profile_id
        )
        run = self.database.get_collection_run(run_id, profile_id=self.profile_id)
        self._emit(
            progress,
            SyncProgress(
                "run_completed", run_id, completed, len(slices),
                resumed=resumed, profile_id=self.profile_id
            ),
        )
        return SyncResult(
            run_id,
            start_utc,
            end_utc,
            "completed",
            completed,
            len(slices),
            run["pages_fetched"],
            None,
            resumed,
            self.profile_id,
        )

    def _to_database_bucket(self, record, collected_at: str) -> UsageBucket:
        return UsageBucket(
            bucket_start_utc=record.bucket_start_utc,
            bucket_end_utc=record.bucket_end_utc,
            project_id=record.project_id_private,
            project_name=None,
            model=record.model,
            service_tier=record.service_tier,
            input_tokens=record.input_tokens,
            cached_input_tokens=record.cached_input_tokens,
            output_tokens=record.output_tokens,
            request_count=record.request_count,
            catalog_version=self.catalog_version,
            collected_at=collected_at,
            project_key=record.project_key,
        )

    @staticmethod
    def _emit(callback: ProgressCallback | None, event: SyncProgress) -> None:
        if callback is not None:
            callback(event)


class HistoryOperations:
    """Explicit facade for destructive retention and database health operations."""

    def __init__(
        self, database: DatabaseService, *, profile_id: str = DEFAULT_PROFILE_ID
    ):
        self.database = database
        self.profile_id = profile_id

    def preview_retention(
        self, retention_days: int, *, now: datetime | None = None
    ) -> RetentionPreview:
        _validate_days(retention_days, maximum=36_500)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        midnight = int(
            current.astimezone(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        cutoff = midnight - retention_days * SECONDS_PER_DAY
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), MIN(bucket_start_utc), MAX(bucket_end_utc)
                FROM usage_buckets WHERE profile_id = ? AND bucket_end_utc <= ?
                """,
                (self.profile_id, cutoff),
            ).fetchone()
        return RetentionPreview(
            str(self.database.path),
            self.profile_id,
            retention_days,
            cutoff,
            int(row[0]),
            row[1],
            row[2],
        )

    def apply_retention(
        self, preview: RetentionPreview, *, confirm: bool = False
    ) -> RetentionResult:
        if not confirm:
            raise ValueError("retention deletion requires explicit confirmation")
        if (
            not isinstance(preview, RetentionPreview)
            or preview.database_path != str(self.database.path)
            or preview.profile_id != self.profile_id
        ):
            raise ValueError("retention preview does not belong to this database")
        with self.database.transaction() as connection:
            current_count = connection.execute(
                """
                SELECT COUNT(*) FROM usage_buckets
                WHERE profile_id = ? AND bucket_end_utc <= ?
                """,
                (self.profile_id, preview.cutoff_utc),
            ).fetchone()[0]
            if current_count != preview.row_count:
                raise ValueError("history changed after preview; create a new retention preview")
            connection.execute(
                "DELETE FROM usage_buckets WHERE profile_id = ? AND bucket_end_utc <= ?",
                (self.profile_id, preview.cutoff_utc),
            )
            deleted = connection.execute("SELECT changes()").fetchone()[0]
        return RetentionResult(preview.cutoff_utc, int(deleted))

    def check_integrity(self, *, full: bool = True) -> IntegrityResult:
        return self.database.check_integrity(full=full)

    def create_backup(self, destination: str | Path) -> Path:
        return self.database.backup_to(destination)

    def restore_backup(self, source: str | Path, destination: str | Path) -> Path:
        return DatabaseService.restore_from_backup(source, destination)


def _validate_days(days: int, *, maximum: int) -> None:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= maximum:
        raise ValueError(f"days must be between 1 and {maximum}")


def _validate_sync_range(start_utc: int, end_utc: int) -> None:
    if (
        isinstance(start_utc, bool)
        or isinstance(end_utc, bool)
        or not isinstance(start_utc, int)
        or not isinstance(end_utc, int)
        or start_utc < 0
        or end_utc <= start_utc
    ):
        raise ValueError("sync range must be a positive Unix timestamp range")
    if end_utc - start_utc > MAX_SYNC_DAYS * SECONDS_PER_DAY:
        raise ValueError(f"sync range cannot exceed {MAX_SYNC_DAYS} days")


def _safe_sync_error_code(exc: Exception) -> str:
    if isinstance(exc, (OpenAIClientError, UpstreamContractError)):
        code = exc.code
        if isinstance(code, str) and _SYNC_ERROR_CODE.fullmatch(code):
            return code
    return "sync_failed"
