"""SQLite history storage for usage collection and export.

All persisted timestamps are UTC.  API timestamps use Unix seconds while audit
timestamps use the RFC 3339 representation produced by :func:`utc_now_text`.
The database never stores an Admin API key, an Authorization header, or an
upstream response body.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .classification import is_incentivized
from .model_catalog import find_model, load_catalog


SCHEMA_VERSION = 2
DEFAULT_PROFILE_ID = "prof_00000000000000000000000000000000"
UNATTRIBUTED_PROJECT_KEY = "unattributed"
UNKNOWN_DIMENSION = "unknown"
MAX_QUERY_DAYS = 366
REPAIR_GUIDANCE = (
    "The history database failed its integrity check and is read-only. "
    "Keep the original file, create a filesystem copy for recovery, or restore a known-good backup."
)
_SAFE_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PROJECT_KEY = re.compile(r"project-[0-9a-f]{24}\Z")
_RUN_STATUSES = frozenset({"running", "completed", "partial", "failed", "cancelled"})
_SLICE_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})


MigrationCheckpoint = Callable[[str, int, int | None], None]


def _noop_migration_checkpoint(_phase: str, _version: int, _statement_index: int | None) -> None:
    """Default migration checkpoint used by production callers."""


def _is_busy_error(error: BaseException) -> bool:
    """Return whether SQLite reported transient lock contention, not corruption."""

    return isinstance(error, sqlite3.OperationalError) and any(
        token in str(error).lower() for token in ("locked", "busy")
    )


class DatabaseError(RuntimeError):
    """Base class for safe, user-actionable history database failures."""


class DatabaseReadOnlyError(DatabaseError):
    """Raised when a mutation is attempted after an integrity failure."""


class MigrationError(DatabaseError):
    """Raised when schema migration cannot be completed atomically."""


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    messages: tuple[str, ...]
    read_only: bool
    guidance: str | None = None


@dataclass(frozen=True)
class UsageBucket:
    bucket_start_utc: int
    bucket_end_utc: int
    project_id: str | None = field(repr=False)
    project_name: str | None
    model: str
    service_tier: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    request_count: int
    catalog_version: str
    collected_at: str
    project_key: str | None = None


def utc_now_text(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch_to_utc_text(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_project_id(project_id: object) -> str | None:
    if project_id is None:
        return None
    if not isinstance(project_id, str):
        raise ValueError("project_id must be a string or null")
    value = project_id.strip()
    return value or None


def mask_project_id(project_id: str | None) -> str:
    normalized = normalize_project_id(project_id)
    if normalized is None:
        return ""
    if len(normalized) <= 8:
        return f"{normalized[:2]}…{normalized[-2:]}"
    return f"{normalized[:5]}…{normalized[-4:]}"


def _dimension(value: object, *, field: str) -> str:
    if value is None:
        return UNKNOWN_DIMENSION
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    normalized = value.strip()
    if not normalized:
        return UNKNOWN_DIMENSION
    if field == "service_tier":
        return normalized.lower().replace("_", "-")
    return normalized


def _nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def normalize_api_buckets(
    api_buckets: Sequence[object],
    *,
    catalog_version: str,
    project_key_deriver: Callable[[object], str],
    collected_at: str | None = None,
    project_names: dict[str, str] | None = None,
) -> list[UsageBucket]:
    """Flatten Usage API buckets without dropping the project dimension.

    Duplicate result dimensions inside an API response are deliberately folded
    before persistence so they map to one normalized database unique key.
    """

    if not isinstance(api_buckets, (list, tuple)):
        raise ValueError("api_buckets must be a list or tuple")
    if not isinstance(catalog_version, str) or not catalog_version.strip():
        raise ValueError("catalog_version must be a non-empty string")
    names = project_names or {}
    stamp = collected_at or utc_now_text()
    normalized: dict[tuple[int, int, str | None, str, str], dict] = {}
    for bucket in api_buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise ValueError("usage bucket must contain a results list")
        start = _nonnegative_integer(bucket.get("start_time"), field="start_time")
        end = _nonnegative_integer(bucket.get("end_time"), field="end_time")
        if end <= start:
            raise ValueError("usage bucket end_time must be after start_time")
        for result in bucket["results"]:
            if not isinstance(result, dict):
                raise ValueError("usage result must be an object")
            project_id = normalize_project_id(result.get("project_id"))
            model = _dimension(result.get("model"), field="model")
            service_tier = _dimension(result.get("service_tier"), field="service_tier")
            input_tokens = _nonnegative_integer(result.get("input_tokens", 0), field="input_tokens")
            cached = _nonnegative_integer(
                result.get("input_cached_tokens", 0), field="input_cached_tokens"
            )
            output_tokens = _nonnegative_integer(result.get("output_tokens", 0), field="output_tokens")
            requests = _nonnegative_integer(
                result.get("num_model_requests", result.get("request_count", 0)),
                field="num_model_requests",
            )
            if cached > input_tokens:
                raise ValueError("input_cached_tokens cannot exceed input_tokens")
            key = (start, end, project_id, model, service_tier)
            item = normalized.setdefault(
                key,
                {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "request_count": 0,
                },
            )
            item["input_tokens"] += input_tokens
            item["cached_input_tokens"] += cached
            item["output_tokens"] += output_tokens
            item["request_count"] += requests

    records = []
    for (start, end, project_id, model, tier), counts in sorted(
        normalized.items(), key=lambda pair: (pair[0][0], pair[0][2] or "", pair[0][3], pair[0][4])
    ):
        project_name = names.get(project_id, None) if project_id is not None else "Unattributed"
        records.append(
            UsageBucket(
                bucket_start_utc=start,
                bucket_end_utc=end,
                project_id=project_id,
                project_name=project_name,
                model=model,
                service_tier=tier,
                input_tokens=counts["input_tokens"],
                cached_input_tokens=counts["cached_input_tokens"],
                output_tokens=counts["output_tokens"],
                request_count=counts["request_count"],
                catalog_version=catalog_version.strip(),
                collected_at=stamp,
                project_key=project_key_deriver(project_id),
            )
        )
    return records


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE projects (
            project_key TEXT PRIMARY KEY NOT NULL,
            project_id_private TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE usage_buckets (
            bucket_start_utc INTEGER NOT NULL,
            bucket_end_utc INTEGER NOT NULL,
            project_key TEXT NOT NULL REFERENCES projects(project_key),
            model TEXT NOT NULL,
            service_tier TEXT NOT NULL,
            input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
            cached_input_tokens INTEGER NOT NULL CHECK (
                cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
            ),
            output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
            request_count INTEGER NOT NULL CHECK (request_count >= 0),
            catalog_version TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            CHECK (bucket_end_utc > bucket_start_utc),
            UNIQUE (bucket_start_utc, bucket_end_utc, project_key, model, service_tier)
        )
        """,
        """
        CREATE TABLE collection_runs (
            run_id TEXT PRIMARY KEY NOT NULL,
            requested_start_utc INTEGER NOT NULL,
            requested_end_utc INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'completed', 'partial', 'failed', 'cancelled')
            ),
            pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
            error_code TEXT,
            CHECK (requested_end_utc > requested_start_utc)
        )
        """,
        """
        CREATE TABLE collection_slices (
            run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE CASCADE,
            slice_start_utc INTEGER NOT NULL,
            slice_end_utc INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'in_progress', 'completed', 'failed')
            ),
            pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
            checkpoint TEXT,
            error_code TEXT,
            updated_at TEXT NOT NULL,
            CHECK (slice_end_utc > slice_start_utc),
            PRIMARY KEY (run_id, slice_start_utc, slice_end_utc)
        )
        """,
        "CREATE INDEX usage_buckets_time_idx ON usage_buckets(bucket_start_utc, bucket_end_utc)",
        "CREATE INDEX usage_buckets_project_time_idx ON usage_buckets(project_key, bucket_start_utc)",
        "CREATE INDEX collection_slices_time_idx ON collection_slices(slice_start_utc, slice_end_utc, status)",
    ),
    2: (
        "ALTER TABLE projects RENAME TO projects_v1",
        "ALTER TABLE usage_buckets RENAME TO usage_buckets_v1",
        "ALTER TABLE collection_runs RENAME TO collection_runs_v1",
        "ALTER TABLE collection_slices RENAME TO collection_slices_v1",
        """
        CREATE TABLE profiles (
            profile_id TEXT PRIMARY KEY NOT NULL,
            display_name TEXT NOT NULL,
            credential_id TEXT NOT NULL UNIQUE,
            organization_ref_private TEXT,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            last_validated_at TEXT
        )
        """,
        f"""
        INSERT INTO profiles(
            profile_id, display_name, credential_id, organization_ref_private,
            enabled, created_at, last_validated_at
        ) VALUES (
            '{DEFAULT_PROFILE_ID}', 'Default profile', 'cred_default_migration_pending',
            NULL, 1, '1970-01-01T00:00:00Z', NULL
        )
        """,
        """
        CREATE TABLE projects (
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
            project_key TEXT NOT NULL,
            project_id_private TEXT NOT NULL,
            display_name TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, project_key),
            UNIQUE (profile_id, project_id_private)
        )
        """,
        """
        CREATE TABLE usage_buckets (
            profile_id TEXT NOT NULL,
            bucket_start_utc INTEGER NOT NULL,
            bucket_end_utc INTEGER NOT NULL,
            project_key TEXT NOT NULL,
            model TEXT NOT NULL,
            service_tier TEXT NOT NULL,
            input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
            cached_input_tokens INTEGER NOT NULL CHECK (
                cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
            ),
            output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
            request_count INTEGER NOT NULL CHECK (request_count >= 0),
            catalog_version TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            CHECK (bucket_end_utc > bucket_start_utc),
            FOREIGN KEY (profile_id, project_key)
                REFERENCES projects(profile_id, project_key),
            UNIQUE (
                profile_id, bucket_start_utc, bucket_end_utc,
                project_key, model, service_tier
            )
        )
        """,
        """
        CREATE TABLE collection_runs (
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL,
            requested_start_utc INTEGER NOT NULL,
            requested_end_utc INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'completed', 'partial', 'failed', 'cancelled')
            ),
            pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
            error_code TEXT,
            CHECK (requested_end_utc > requested_start_utc),
            PRIMARY KEY (profile_id, run_id)
        )
        """,
        """
        CREATE TABLE collection_slices (
            profile_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            slice_start_utc INTEGER NOT NULL,
            slice_end_utc INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'in_progress', 'completed', 'failed')
            ),
            pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
            checkpoint TEXT,
            error_code TEXT,
            updated_at TEXT NOT NULL,
            CHECK (slice_end_utc > slice_start_utc),
            FOREIGN KEY (profile_id, run_id)
                REFERENCES collection_runs(profile_id, run_id) ON DELETE CASCADE,
            PRIMARY KEY (profile_id, run_id, slice_start_utc, slice_end_utc)
        )
        """,
        """
        CREATE TABLE alert_rules (
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
            rule_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            project_key TEXT NOT NULL DEFAULT 'all',
            threshold_percent REAL NOT NULL CHECK (
                threshold_percent > 0 AND threshold_percent <= 100
            ),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, rule_id)
        )
        """,
        """
        CREATE TABLE alert_dedup (
            profile_id TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            utc_day TEXT NOT NULL,
            group_id TEXT NOT NULL,
            project_key TEXT NOT NULL,
            previous_percent REAL NOT NULL CHECK (previous_percent >= 0),
            sent_at TEXT,
            PRIMARY KEY (profile_id, rule_id, utc_day),
            FOREIGN KEY (profile_id, rule_id)
                REFERENCES alert_rules(profile_id, rule_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE notification_history (
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
            notification_id TEXT NOT NULL,
            rule_id TEXT,
            event_kind TEXT NOT NULL,
            group_id TEXT NOT NULL,
            project_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL CHECK (
                delivery_status IN ('sent', 'failed', 'suppressed', 'test')
            ),
            error_code TEXT,
            is_test INTEGER NOT NULL CHECK (is_test IN (0, 1)),
            PRIMARY KEY (profile_id, notification_id)
        )
        """,
        f"""
        INSERT INTO projects
        SELECT '{DEFAULT_PROFILE_ID}', project_key, project_id_private, display_name,
               first_seen_at, last_seen_at
        FROM projects_v1
        """,
        f"""
        INSERT INTO usage_buckets
        SELECT '{DEFAULT_PROFILE_ID}', bucket_start_utc, bucket_end_utc, project_key,
               model, service_tier, input_tokens, cached_input_tokens, output_tokens,
               request_count, catalog_version, collected_at
        FROM usage_buckets_v1
        """,
        f"""
        INSERT INTO collection_runs
        SELECT '{DEFAULT_PROFILE_ID}', run_id, requested_start_utc, requested_end_utc,
               started_at, finished_at, status, pages_fetched, error_code
        FROM collection_runs_v1
        """,
        f"""
        INSERT INTO collection_slices
        SELECT '{DEFAULT_PROFILE_ID}', run_id, slice_start_utc, slice_end_utc,
               status, pages_fetched, checkpoint, error_code, updated_at
        FROM collection_slices_v1
        """,
        "DROP TABLE collection_slices_v1",
        "DROP TABLE usage_buckets_v1",
        "DROP TABLE collection_runs_v1",
        "DROP TABLE projects_v1",
        """
        CREATE INDEX usage_buckets_time_idx
        ON usage_buckets(profile_id, bucket_start_utc, bucket_end_utc)
        """,
        """
        CREATE INDEX usage_buckets_project_time_idx
        ON usage_buckets(profile_id, project_key, bucket_start_utc)
        """,
        """
        CREATE INDEX collection_slices_time_idx
        ON collection_slices(profile_id, slice_start_utc, slice_end_utc, status)
        """,
        """
        CREATE INDEX notification_history_time_idx
        ON notification_history(profile_id, occurred_at)
        """,
    ),
}


class DatabaseService:
    """Single entry point for SQLite connections, migrations, and transactions."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        migration_checkpoint: MigrationCheckpoint | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.busy_timeout_ms = busy_timeout_ms
        self._read_only = False
        self._integrity_messages: tuple[str, ...] = ()
        self._last_migration_backup: tuple[Path, Path] | None = None
        self._migration_checkpoint = migration_checkpoint or _noop_migration_checkpoint
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if self.path.exists() and self.path.stat().st_size:
                result = self._probe_integrity()
                if not result.ok:
                    self._read_only = True
                    self._integrity_messages = result.messages
                    return
            self._apply_migrations()
        except MigrationError:
            raise
        except (sqlite3.DatabaseError, OSError, DatabaseError) as exc:
            raise MigrationError("The history database schema could not be initialized.") from exc

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    @property
    def schema_version(self) -> int:
        if self._read_only:
            return 0
        with self.connection() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @property
    def last_migration_backup(self) -> tuple[Path, Path] | None:
        return self._last_migration_backup

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if write:
            self._ensure_writable()
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        else:
            if self.path.exists():
                uri = self.path.as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
            else:
                connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if write:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        else:
            connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect(write=write)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise DatabaseReadOnlyError(REPAIR_GUIDANCE)

    def _probe_integrity(self) -> IntegrityResult:
        try:
            with self.connection() as connection:
                rows = connection.execute("PRAGMA integrity_check").fetchall()
            messages = tuple(str(row[0]) for row in rows)
            ok = messages == ("ok",)
        except (sqlite3.DatabaseError, OSError) as exc:
            if _is_busy_error(exc):
                raise
            messages = ("database file could not be read",)
            ok = False
        return IntegrityResult(ok, messages, not ok, None if ok else REPAIR_GUIDANCE)

    def check_integrity(self, *, full: bool = False) -> IntegrityResult:
        pragma = "integrity_check" if full else "quick_check"
        try:
            with self.connection() as connection:
                rows = connection.execute(f"PRAGMA {pragma}").fetchall()
            messages = tuple(str(row[0]) for row in rows)
            ok = messages == ("ok",)
        except (sqlite3.DatabaseError, OSError) as exc:
            if _is_busy_error(exc):
                return IntegrityResult(
                    False,
                    ("database is busy",),
                    self._read_only,
                    "Close other database users and retry the integrity check.",
                )
            messages = ("database file could not be read",)
            ok = False
        if not ok:
            self._read_only = True
            self._integrity_messages = messages
        return IntegrityResult(ok, messages, self._read_only, None if ok else REPAIR_GUIDANCE)

    def _apply_migrations(self) -> None:
        connection = self._connect(write=True)
        try:
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            current = 0
            if has_migrations:
                row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                current = int(row[0] or 0)
            if current > SCHEMA_VERSION:
                raise MigrationError(
                    f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}."
                )
            connection.execute("BEGIN IMMEDIATE")
            if current == 1 and SCHEMA_VERSION >= 2:
                self._last_migration_backup = self._create_migration_backup(1, 2)
                self._migration_checkpoint("backup-created", 2, None)
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise MigrationError(f"Missing database migration {version}.")
                for statement_index, statement in enumerate(statements):
                    self._migration_checkpoint("before-statement", version, statement_index)
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now_text()),
                )
            self._migration_checkpoint("before-commit", SCHEMA_VERSION, None)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_migration_backup(self, from_version: int, to_version: int) -> tuple[Path, Path]:
        suffix = uuid.uuid4().hex[:8]
        backup_path = self.path.with_name(
            f"{self.path.name}.schema-v{from_version}-to-v{to_version}.{suffix}.backup"
        )
        self.backup_to(backup_path)
        hasher = hashlib.sha256()
        with backup_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".metadata.json")
        metadata = {
            "schema_from": from_version,
            "schema_to": to_version,
            "created_at": utc_now_text(),
            "backup_file": backup_path.name,
            "sha256": digest,
        }
        _atomic_bytes_write(
            metadata_path,
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return backup_path, metadata_path

    def backup_to(self, destination: str | Path) -> Path:
        """Create and atomically publish a consistent SQLite snapshot."""

        self._ensure_writable()
        target = Path(destination).expanduser().resolve()
        if target == self.path:
            raise ValueError("backup destination must differ from the database path")
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            with self.connection() as source:
                backup = sqlite3.connect(temporary)
                try:
                    source.backup(backup)
                    check = backup.execute("PRAGMA quick_check").fetchall()
                    if check != [("ok",)]:
                        raise DatabaseError("The database backup failed its integrity check.")
                finally:
                    backup.close()
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            return target
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def restore_from_backup(source: str | Path, destination: str | Path) -> Path:
        """Atomically restore a verified SQLite snapshot without changing the source."""

        backup_path = Path(source).expanduser().resolve()
        target = Path(destination).expanduser().resolve()
        if not backup_path.is_file():
            raise FileNotFoundError("database backup was not found")
        if backup_path == target:
            raise ValueError("restore destination must differ from the backup path")
        if target.exists():
            raise FileExistsError("restore destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source_uri = backup_path.as_uri() + "?mode=ro"
            source_connection = sqlite3.connect(source_uri, uri=True)
            restored = sqlite3.connect(temporary)
            try:
                check = source_connection.execute("PRAGMA quick_check").fetchall()
                if check != [("ok",)]:
                    raise DatabaseError("The database backup failed its integrity check.")
                source_connection.backup(restored)
                restored_check = restored.execute("PRAGMA quick_check").fetchall()
                if restored_check != [("ok",)]:
                    raise DatabaseError("The restored database failed its integrity check.")
            finally:
                restored.close()
                source_connection.close()
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            return target
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def replace_from_backup(self, source: str | Path) -> Path:
        """Verify and atomically replace this database from a backup.

        Callers must provide their own exclusive/offline operation lock. This
        object owns no persistent SQLite connection, so after the replacement
        subsequent operations open the restored file.
        """

        target = self.path
        staging = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.sqlite3")
        try:
            self.restore_from_backup(source, staging)
            try:
                connection = sqlite3.connect(target, timeout=self.busy_timeout_ms / 1000)
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError):
                # A corrupt target may not be openable; the verified staging
                # database remains authoritative for an explicit restore.
                pass
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            os.replace(staging, target)
            self._read_only = False
            self._integrity_messages = ()
            self._apply_migrations()
            result = self.check_integrity(full=True)
            if not result.ok:
                raise DatabaseError("The restored database failed its integrity check.")
            return target
        finally:
            staging.unlink(missing_ok=True)

    def start_collection_run(
        self,
        requested_start_utc: int,
        requested_end_utc: int,
        *,
        run_id: str | None = None,
        started_at: str | None = None,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> str:
        _validate_range(requested_start_utc, requested_end_utc)
        profile_id = _validate_profile_id(profile_id)
        identifier = run_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("run_id must be a non-empty string of at most 128 characters")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs(
                    profile_id, run_id, requested_start_utc, requested_end_utc, started_at, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (
                    profile_id,
                    identifier,
                    requested_start_utc,
                    requested_end_utc,
                    started_at or utc_now_text(),
                ),
            )
        return identifier

    def record_slice_checkpoint(
        self,
        run_id: str,
        slice_start_utc: int,
        slice_end_utc: int,
        *,
        checkpoint: str | None,
        pages_fetched: int,
        status: str = "in_progress",
        error_code: str | None = None,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        _validate_range(slice_start_utc, slice_end_utc)
        if status not in _SLICE_STATUSES - {"completed"}:
            raise ValueError("checkpoint status must be pending, in_progress, or failed")
        pages = _nonnegative_integer(pages_fetched, field="pages_fetched")
        safe_error = _safe_error_code(error_code)
        profile_id = _validate_profile_id(profile_id)
        if checkpoint is not None and (not isinstance(checkpoint, str) or len(checkpoint) > 2048):
            raise ValueError("checkpoint must be null or a string of at most 2048 characters")
        with self.transaction() as connection:
            _assert_slice_in_run(connection, profile_id, run_id, slice_start_utc, slice_end_utc)
            connection.execute(
                """
                INSERT INTO collection_slices(
                    profile_id, run_id, slice_start_utc, slice_end_utc, status,
                    pages_fetched, checkpoint, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, run_id, slice_start_utc, slice_end_utc) DO UPDATE SET
                    status = excluded.status,
                    pages_fetched = excluded.pages_fetched,
                    checkpoint = excluded.checkpoint,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    profile_id,
                    run_id,
                    slice_start_utc,
                    slice_end_utc,
                    status,
                    pages,
                    checkpoint,
                    safe_error,
                    utc_now_text(),
                ),
            )
            connection.execute(
                """
                UPDATE collection_runs
                SET pages_fetched = (
                    SELECT COALESCE(SUM(pages_fetched), 0)
                    FROM collection_slices WHERE profile_id = ? AND run_id = ?
                )
                WHERE profile_id = ? AND run_id = ?
                """,
                (profile_id, run_id, profile_id, run_id),
            )

    def resume_checkpoint(
        self,
        run_id: str,
        slice_start_utc: int,
        slice_end_utc: int,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> dict | None:
        profile_id = _validate_profile_id(profile_id)
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT status, pages_fetched, checkpoint, error_code, updated_at
                FROM collection_slices
                WHERE profile_id = ? AND run_id = ?
                  AND slice_start_utc = ? AND slice_end_utc = ?
                """,
                (profile_id, run_id, slice_start_utc, slice_end_utc),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_slice(
        self,
        run_id: str,
        slice_start_utc: int,
        slice_end_utc: int,
        buckets: Iterable[UsageBucket],
        *,
        pages_fetched: int,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int:
        """Atomically replace one complete API slice and mark it complete.

        Callers retain partial pages outside this method.  Therefore a timeout
        cannot commit half a slice or accidentally mark it complete.
        """

        _validate_range(slice_start_utc, slice_end_utc)
        pages = _nonnegative_integer(pages_fetched, field="pages_fetched")
        profile_id = _validate_profile_id(profile_id)
        records = [_validate_bucket(item, slice_start_utc, slice_end_utc) for item in buckets]
        with self.transaction() as connection:
            _assert_slice_in_run(connection, profile_id, run_id, slice_start_utc, slice_end_utc)
            connection.execute("DROP TABLE IF EXISTS temp.staged_usage_buckets")
            connection.execute(
                """
                CREATE TEMP TABLE staged_usage_buckets (
                    bucket_start_utc INTEGER NOT NULL,
                    bucket_end_utc INTEGER NOT NULL,
                    project_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    service_tier TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    catalog_version TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY (
                        bucket_start_utc, bucket_end_utc, project_key, model, service_tier
                    )
                ) WITHOUT ROWID
                """
            )
            for bucket in records:
                project_id = normalize_project_id(bucket.project_id)
                project_key = _normalized_project_key(bucket.project_key, project_id)
                project_private = project_id or ""
                display_name = _project_display_name(project_id, bucket.project_name)
                connection.execute(
                    """
                    INSERT INTO projects(
                        profile_id, project_key, project_id_private,
                        display_name, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, project_key) DO UPDATE SET
                        display_name = CASE
                            WHEN excluded.display_name <> '' THEN excluded.display_name
                            ELSE projects.display_name
                        END,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        profile_id,
                        project_key,
                        project_private,
                        display_name,
                        bucket.collected_at,
                        bucket.collected_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO staged_usage_buckets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        bucket_start_utc, bucket_end_utc, project_key, model, service_tier
                    ) DO UPDATE SET
                        input_tokens = staged_usage_buckets.input_tokens + excluded.input_tokens,
                        cached_input_tokens = staged_usage_buckets.cached_input_tokens + excluded.cached_input_tokens,
                        output_tokens = staged_usage_buckets.output_tokens + excluded.output_tokens,
                        request_count = staged_usage_buckets.request_count + excluded.request_count,
                        catalog_version = excluded.catalog_version,
                        collected_at = excluded.collected_at
                    """,
                    (
                        bucket.bucket_start_utc,
                        bucket.bucket_end_utc,
                        project_key,
                        bucket.model,
                        bucket.service_tier,
                        bucket.input_tokens,
                        bucket.cached_input_tokens,
                        bucket.output_tokens,
                        bucket.request_count,
                        bucket.catalog_version,
                        bucket.collected_at,
                    ),
                )

            connection.execute(
                """
                DELETE FROM usage_buckets
                WHERE profile_id = ?
                  AND bucket_start_utc >= ? AND bucket_end_utc <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM staged_usage_buckets staged
                    WHERE staged.bucket_start_utc = usage_buckets.bucket_start_utc
                      AND staged.bucket_end_utc = usage_buckets.bucket_end_utc
                      AND staged.project_key = usage_buckets.project_key
                      AND staged.model = usage_buckets.model
                      AND staged.service_tier = usage_buckets.service_tier
                  )
                """,
                (profile_id, slice_start_utc, slice_end_utc),
            )
            connection.execute(
                """
                INSERT INTO usage_buckets(
                    profile_id, bucket_start_utc, bucket_end_utc, project_key,
                    model, service_tier, input_tokens, cached_input_tokens,
                    output_tokens, request_count, catalog_version, collected_at
                )
                SELECT ?, staged_usage_buckets.* FROM staged_usage_buckets
                WHERE 1
                ON CONFLICT(
                    profile_id, bucket_start_utc, bucket_end_utc,
                    project_key, model, service_tier
                ) DO UPDATE SET
                    input_tokens = excluded.input_tokens,
                    cached_input_tokens = excluded.cached_input_tokens,
                    output_tokens = excluded.output_tokens,
                    request_count = excluded.request_count,
                    catalog_version = excluded.catalog_version,
                    collected_at = excluded.collected_at
                """,
                (profile_id,),
            )
            connection.execute(
                """
                INSERT INTO collection_slices(
                    profile_id, run_id, slice_start_utc, slice_end_utc, status,
                    pages_fetched, checkpoint, error_code, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, NULL, NULL, ?)
                ON CONFLICT(profile_id, run_id, slice_start_utc, slice_end_utc) DO UPDATE SET
                    status = 'completed', pages_fetched = excluded.pages_fetched,
                    checkpoint = NULL, error_code = NULL, updated_at = excluded.updated_at
                """,
                (profile_id, run_id, slice_start_utc, slice_end_utc, pages, utc_now_text()),
            )
            connection.execute(
                """
                UPDATE collection_runs
                SET pages_fetched = (
                    SELECT COALESCE(SUM(pages_fetched), 0)
                    FROM collection_slices WHERE profile_id = ? AND run_id = ?
                )
                WHERE profile_id = ? AND run_id = ?
                """,
                (profile_id, run_id, profile_id, run_id),
            )
            stored = connection.execute("SELECT COUNT(*) FROM staged_usage_buckets").fetchone()[0]
        return int(stored)

    def finish_collection_run(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str | None = None,
        finished_at: str | None = None,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        if status not in _RUN_STATUSES - {"running"}:
            raise ValueError("invalid terminal collection run status")
        safe_error = _safe_error_code(error_code)
        profile_id = _validate_profile_id(profile_id)
        with self.transaction() as connection:
            run = connection.execute(
                """
                SELECT requested_start_utc, requested_end_utc FROM collection_runs
                WHERE profile_id = ? AND run_id = ?
                """,
                (profile_id, run_id),
            ).fetchone()
            if run is None:
                raise KeyError("collection run not found")
            if status == "completed" and not _run_has_complete_coverage(
                connection, profile_id, run_id, run[0], run[1]
            ):
                raise ValueError("completed run must have complete, gap-free slice coverage")
            connection.execute(
                """
                UPDATE collection_runs
                SET status = ?, error_code = ?, finished_at = ?
                WHERE profile_id = ? AND run_id = ?
                """,
                (status, safe_error, finished_at or utc_now_text(), profile_id, run_id),
            )

    def get_collection_run(
        self, run_id: str, *, profile_id: str = DEFAULT_PROFILE_ID
    ) -> dict | None:
        profile_id = _validate_profile_id(profile_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE profile_id = ? AND run_id = ?",
                (profile_id, run_id),
            ).fetchone()
        return dict(row) if row else None

    def find_resumable_collection_run(
        self,
        requested_start_utc: int,
        requested_end_utc: int,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> dict | None:
        """Return the newest exact-range run that has not completed."""

        _validate_range(requested_start_utc, requested_end_utc)
        profile_id = _validate_profile_id(profile_id)
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM collection_runs
                WHERE profile_id = ?
                  AND requested_start_utc = ? AND requested_end_utc = ?
                  AND status IN ('running', 'partial', 'failed')
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (profile_id, requested_start_utc, requested_end_utc),
            ).fetchone()
        return dict(row) if row else None

    def resume_collection_run(
        self, run_id: str, *, profile_id: str = DEFAULT_PROFILE_ID
    ) -> None:
        profile_id = _validate_profile_id(profile_id)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM collection_runs WHERE profile_id = ? AND run_id = ?",
                (profile_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError("collection run not found")
            if row[0] == "completed" or row[0] == "cancelled":
                raise ValueError("terminal collection run cannot be resumed")
            connection.execute(
                """
                UPDATE collection_runs
                SET status = 'running', finished_at = NULL, error_code = NULL
                WHERE profile_id = ? AND run_id = ?
                """,
                (profile_id, run_id),
            )

    def list_collection_slices(
        self, run_id: str, *, profile_id: str = DEFAULT_PROFILE_ID
    ) -> list[dict]:
        profile_id = _validate_profile_id(profile_id)
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT slice_start_utc, slice_end_utc, status,
                       pages_fetched, checkpoint, error_code, updated_at
                FROM collection_slices WHERE profile_id = ? AND run_id = ?
                ORDER BY slice_start_utc, slice_end_utc
                """,
                (profile_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_projects(self, *, profile_id: str = DEFAULT_PROFILE_ID) -> list[dict]:
        profile_id = _validate_profile_id(profile_id)
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT projects.project_key, projects.display_name,
                       projects.first_seen_at, projects.last_seen_at,
                       CASE WHEN projects.project_key = ? THEN '' ELSE projects.project_id_private END AS project_id_private,
                       COUNT(usage_buckets.project_key) AS bucket_count
                FROM projects
                LEFT JOIN usage_buckets
                  ON usage_buckets.profile_id = projects.profile_id
                 AND usage_buckets.project_key = projects.project_key
                WHERE projects.profile_id = ?
                GROUP BY projects.profile_id, projects.project_key
                ORDER BY CASE WHEN projects.project_key = ? THEN 1 ELSE 0 END,
                         display_name COLLATE NOCASE, projects.project_key
                """,
                (UNATTRIBUTED_PROJECT_KEY, profile_id, UNATTRIBUTED_PROJECT_KEY),
            ).fetchall()
        return [dict(row) for row in rows]

    def query_usage(
        self,
        start_utc: int,
        end_utc: int,
        *,
        project_key: str | None = None,
        max_days: int = MAX_QUERY_DAYS,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> list[dict]:
        _validate_query_range(start_utc, end_utc, max_days=max_days)
        profile_id = _validate_profile_id(profile_id)
        params: list[object] = [profile_id, start_utc, end_utc]
        project_clause = ""
        if project_key is not None:
            project_clause = "AND usage_buckets.project_key = ?"
            params.append(project_key)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT usage_buckets.*, projects.project_id_private, projects.display_name AS project_name
                FROM usage_buckets
                JOIN projects
                  ON projects.profile_id = usage_buckets.profile_id
                 AND projects.project_key = usage_buckets.project_key
                WHERE usage_buckets.profile_id = ?
                  AND bucket_start_utc >= ? AND bucket_end_utc <= ?
                  {project_clause}
                ORDER BY bucket_start_utc, project_key, model, service_tier
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["project_id"] = item.pop("project_id_private") or None
            item["total_tokens"] = item["input_tokens"] + item["output_tokens"]
            result.append(item)
        return result

    def daily_usage(
        self,
        start_utc: int,
        end_utc: int,
        *,
        project_key: str | None = None,
        catalog: dict | None = None,
        include_missing: bool = False,
        max_days: int = MAX_QUERY_DAYS,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> list[dict]:
        """Aggregate fixed UTC days with a completeness state for each day."""

        _validate_query_range(start_utc, end_utc, max_days=max_days)
        if start_utc % 86_400 or end_utc % 86_400:
            raise ValueError("daily query boundaries must be 00:00:00Z")
        active_catalog = catalog or load_catalog()
        profile_id = _validate_profile_id(profile_id)
        records = self.query_usage(
            start_utc,
            end_utc,
            project_key=project_key,
            max_days=max_days,
            profile_id=profile_id,
        )
        with self.connection() as connection:
            slices = connection.execute(
                """
                SELECT slice_start_utc, slice_end_utc, status
                FROM collection_slices
                WHERE profile_id = ?
                  AND slice_end_utc > ? AND slice_start_utc < ?
                ORDER BY slice_start_utc, slice_end_utc
                """,
                (profile_id, start_utc, end_utc),
            ).fetchall()

        by_day: dict[int, list[dict]] = {}
        for item in records:
            day = item["bucket_start_utc"] - item["bucket_start_utc"] % 86_400
            by_day.setdefault(day, []).append(item)
        output = []
        for day in range(start_utc, end_utc, 86_400):
            next_day = day + 86_400
            rows = by_day.get(day, [])
            complete = _interval_is_covered(
                day,
                next_day,
                [(row[0], row[1]) for row in slices if row[2] == "completed"],
            )
            overlaps = any(row[0] < next_day and row[1] > day for row in slices)
            completeness = "complete" if complete else "partial" if rows or overlaps else "missing"
            if completeness == "missing" and not include_missing:
                continue
            has_known_total = complete or bool(rows)
            groups = {group_id: 0 for group_id in active_catalog["groups"]}
            other_tokens = 0
            input_tokens = 0
            cached_input_tokens = 0
            output_tokens = 0
            request_count = 0
            for row in rows:
                total = row["input_tokens"] + row["output_tokens"]
                entry = find_model(row["model"], active_catalog)
                if is_incentivized(row["service_tier"]) and entry and entry.get("eligible", True):
                    groups[entry["group"]] += total
                else:
                    other_tokens += total
                input_tokens += row["input_tokens"]
                cached_input_tokens += row["cached_input_tokens"]
                output_tokens += row["output_tokens"]
                request_count += row["request_count"]
            output.append(
                {
                    "day_start_utc": day,
                    "day": epoch_to_utc_text(day)[:10],
                    "groups": groups if has_known_total else None,
                    "other_tokens": other_tokens if has_known_total else None,
                    "input_tokens": input_tokens if has_known_total else None,
                    "cached_input_tokens": cached_input_tokens if has_known_total else None,
                    "output_tokens": output_tokens if has_known_total else None,
                    "total_tokens": input_tokens + output_tokens if has_known_total else None,
                    "request_count": request_count if has_known_total else None,
                    "completeness": completeness,
                }
            )
        return output


def _safe_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_ERROR_CODE.fullmatch(value):
        raise ValueError("error_code must be a sanitized lowercase code")
    return value


def _validate_profile_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"prof_[0-9a-f]{32}", value):
        raise ValueError("profile_id must be an opaque prof_ identifier")
    return value


def _validate_range(start: int, end: int) -> None:
    _nonnegative_integer(start, field="start_utc")
    _nonnegative_integer(end, field="end_utc")
    if end <= start:
        raise ValueError("end_utc must be after start_utc")


def _validate_query_range(start: int, end: int, *, max_days: int) -> None:
    _validate_range(start, end)
    if isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= MAX_QUERY_DAYS:
        raise ValueError(f"max_days must be between 1 and {MAX_QUERY_DAYS}")
    if end - start > max_days * 86_400:
        raise ValueError(f"query range cannot exceed {max_days} days")


def _project_display_name(project_id: str | None, project_name: str | None) -> str:
    if project_id is None:
        return "Unattributed"
    if project_name is not None:
        if not isinstance(project_name, str):
            raise ValueError("project_name must be a string or null")
        value = project_name.strip()
        if value:
            return value
    return f"Project {mask_project_id(project_id)}"


def _normalized_project_key(project_key: str | None, project_id: str | None) -> str:
    if project_key is None:
        if project_id is None:
            return UNATTRIBUTED_PROJECT_KEY
        raise ValueError("attributed usage requires a keyed project pseudonym")
    if project_key == UNATTRIBUTED_PROJECT_KEY:
        if project_id is not None:
            raise ValueError("unattributed project key cannot have a project_id")
        return project_key
    if not isinstance(project_key, str) or not _PROJECT_KEY.fullmatch(project_key):
        raise ValueError("project_key must be an unattributed or pseudonymous project key")
    if project_id is None:
        raise ValueError("attributed project_key requires its private project_id")
    return project_key


def _validate_bucket(bucket: UsageBucket, start: int, end: int) -> UsageBucket:
    if not isinstance(bucket, UsageBucket):
        raise TypeError("buckets must contain UsageBucket values")
    if bucket.bucket_start_utc < start or bucket.bucket_end_utc > end:
        raise ValueError("usage bucket falls outside its collection slice")
    _validate_range(bucket.bucket_start_utc, bucket.bucket_end_utc)
    normalized_model = _dimension(bucket.model, field="model")
    normalized_tier = _dimension(bucket.service_tier, field="service_tier")
    values = (
        _nonnegative_integer(bucket.input_tokens, field="input_tokens"),
        _nonnegative_integer(bucket.cached_input_tokens, field="cached_input_tokens"),
        _nonnegative_integer(bucket.output_tokens, field="output_tokens"),
        _nonnegative_integer(bucket.request_count, field="request_count"),
    )
    if values[1] > values[0]:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    if not isinstance(bucket.catalog_version, str) or not bucket.catalog_version.strip():
        raise ValueError("catalog_version must be a non-empty string")
    if not isinstance(bucket.collected_at, str) or not bucket.collected_at:
        raise ValueError("collected_at must be a non-empty string")
    return UsageBucket(
        bucket.bucket_start_utc,
        bucket.bucket_end_utc,
        normalize_project_id(bucket.project_id),
        bucket.project_name,
        normalized_model,
        normalized_tier,
        *values,
        bucket.catalog_version.strip(),
        bucket.collected_at,
        _normalized_project_key(bucket.project_key, normalize_project_id(bucket.project_id)),
    )


def _assert_slice_in_run(
    connection: sqlite3.Connection,
    profile_id: str,
    run_id: str,
    slice_start_utc: int,
    slice_end_utc: int,
) -> None:
    row = connection.execute(
        """
        SELECT requested_start_utc, requested_end_utc, status FROM collection_runs
        WHERE profile_id = ? AND run_id = ?
        """,
        (profile_id, run_id),
    ).fetchone()
    if row is None:
        raise KeyError("collection run not found")
    if row[2] != "running":
        raise ValueError("collection run is not running")
    if slice_start_utc < row[0] or slice_end_utc > row[1]:
        raise ValueError("collection slice falls outside its requested run range")


def _interval_is_covered(start: int, end: int, intervals: Iterable[tuple[int, int]]) -> bool:
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return False


def _run_has_complete_coverage(
    connection: sqlite3.Connection, profile_id: str, run_id: str, start: int, end: int
) -> bool:
    rows = connection.execute(
        """
        SELECT slice_start_utc, slice_end_utc FROM collection_slices
        WHERE profile_id = ? AND run_id = ? AND status = 'completed'
        ORDER BY slice_start_utc, slice_end_utc
        """,
        (profile_id, run_id),
    ).fetchall()
    return _interval_is_covered(start, end, ((row[0], row[1]) for row in rows))


def _atomic_bytes_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
