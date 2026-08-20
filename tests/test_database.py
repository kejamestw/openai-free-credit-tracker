import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import quota_monitor.database as database_module
from quota_monitor.database import (
    MAX_QUERY_DAYS,
    DatabaseReadOnlyError,
    DatabaseService,
    MigrationError,
    UsageBucket,
    normalize_api_buckets,
)
from quota_monitor.model_catalog import load_catalog
from quota_monitor.upstream_adapter import ProjectKeyDeriver


DAY = 86_400
DAY_1 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
STAMP = "2026-01-02T03:04:05Z"
CATALOG = load_catalog()
PROJECT_KEYS = ProjectKeyDeriver(b"0123456789abcdef")


def make_bucket(
    *,
    start=DAY_1,
    project_id="proj_alpha1234",
    project_name="Alpha",
    model="gpt-5.4-mini-2026-03-17",
    tier="incentivized-tier",
    input_tokens=100,
    cached_tokens=20,
    output_tokens=10,
    requests=1,
):
    return UsageBucket(
        bucket_start_utc=start,
        bucket_end_utc=start + 3600,
        project_id=project_id,
        project_name=project_name,
        model=model,
        service_tier=tier,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        request_count=requests,
        catalog_version="2026-01-01",
        collected_at=STAMP,
        project_key=PROJECT_KEYS.derive(project_id),
    )


def reconcile_day(database, buckets, *, day=DAY_1, run_id="run-1", pages=1):
    database.start_collection_run(day, day + DAY, run_id=run_id, started_at=STAMP)
    count = database.reconcile_slice(run_id, day, day + DAY, buckets, pages_fetched=pages)
    database.finish_collection_run(run_id, "completed", finished_at=STAMP)
    return count


def test_schema_migration_is_idempotent_and_configures_connections(tmp_path):
    path = tmp_path / "history.sqlite3"
    database = DatabaseService(path, busy_timeout_ms=3210)
    assert database.schema_version == 2
    assert DatabaseService(path).schema_version == 2

    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 3210
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        migrations = connection.execute("SELECT version FROM schema_migrations").fetchall()
        table_info = connection.execute("PRAGMA table_info(usage_buckets)").fetchall()
        indexes = connection.execute("PRAGMA index_list(usage_buckets)").fetchall()

    assert [row[0] for row in migrations] == [1, 2]
    dimension_columns = {row[1]: row[3] for row in table_info}
    assert all(dimension_columns[name] == 1 for name in ("project_key", "model", "service_tier"))
    assert any(row[2] == 1 for row in indexes), "usage buckets must have a unique index"


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    DatabaseService(path)
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 3)
    monkeypatch.setitem(
        database_module._MIGRATIONS,
        3,
        ("CREATE TABLE should_roll_back(value TEXT)", "THIS IS NOT SQL"),
    )

    with pytest.raises(MigrationError):
        DatabaseService(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'should_roll_back'"
        ).fetchone() is None
    finally:
        connection.close()


def test_database_transaction_rolls_back_on_failure(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    with pytest.raises(RuntimeError, match="stop"):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                (database_module.DEFAULT_PROFILE_ID, "key", "private", "name", STAMP, STAMP),
            )
            raise RuntimeError("stop")

    assert database.list_projects() == []


def test_api_normalization_retains_project_and_folds_duplicate_dimensions():
    records = normalize_api_buckets(
        [
            {
                "start_time": DAY_1,
                "end_time": DAY_1 + 3600,
                "results": [
                    {
                        "project_id": " proj_alpha1234 ",
                        "model": "gpt-5.4-mini-2026-03-17",
                        "service_tier": "INCENTIVIZED_TIER",
                        "input_tokens": 10,
                        "input_cached_tokens": 2,
                        "output_tokens": 1,
                        "num_model_requests": 1,
                    },
                    {
                        "project_id": "proj_alpha1234",
                        "model": "gpt-5.4-mini-2026-03-17",
                        "service_tier": "incentivized-tier",
                        "input_tokens": 20,
                        "input_cached_tokens": 3,
                        "output_tokens": 2,
                        "num_model_requests": 2,
                    },
                    {
                        "project_id": None,
                        "model": None,
                        "service_tier": None,
                        "input_tokens": 5,
                        "output_tokens": 1,
                    },
                ],
            }
        ],
        catalog_version="catalog-v1",
        project_key_deriver=PROJECT_KEYS.derive,
        collected_at=STAMP,
        project_names={"proj_alpha1234": "Alpha"},
    )

    assert len(records) == 2
    attributed = next(record for record in records if record.project_id)
    assert attributed.project_name == "Alpha"
    assert attributed.service_tier == "incentivized-tier"
    assert (attributed.input_tokens, attributed.cached_input_tokens) == (30, 5)
    assert (attributed.output_tokens, attributed.request_count) == (3, 3)
    unattributed = next(record for record in records if record.project_id is None)
    assert (unattributed.model, unattributed.service_tier) == ("unknown", "unknown")


def test_staged_reconcile_is_idempotent_updates_corrections_and_removes_stale_rows(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    first = make_bucket(input_tokens=100)
    stale = make_bucket(model="gpt-5.4-nano", input_tokens=50)
    assert reconcile_day(database, [first, stale], run_id="run-1") == 2
    assert reconcile_day(database, [first, stale], run_id="run-2") == 2
    assert len(database.query_usage(DAY_1, DAY_1 + DAY)) == 2

    corrected = make_bucket(input_tokens=175)
    assert reconcile_day(database, [corrected], run_id="run-3") == 1
    rows = database.query_usage(DAY_1, DAY_1 + DAY)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 175
    assert rows[0]["total_tokens"] == 185


def test_external_hmac_project_key_is_canonical_while_private_id_remains_available(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    hmac_key = "project-0123456789abcdef01234567"
    value = make_bucket(project_id="project-private-a")
    value = UsageBucket(**{**value.__dict__, "project_key": hmac_key})
    reconcile_day(database, [value])

    rows = database.query_usage(DAY_1, DAY_1 + DAY)
    assert rows[0]["project_key"] == hmac_key
    assert rows[0]["project_id"] == "project-private-a"
    assert "project-private-a" not in repr(value)


def test_checkpoint_and_failed_slice_do_not_commit_partial_usage(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    database.start_collection_run(DAY_1, DAY_1 + DAY, run_id="run-timeout", started_at=STAMP)
    database.record_slice_checkpoint(
        "run-timeout",
        DAY_1,
        DAY_1 + DAY,
        checkpoint="opaque-page-2",
        pages_fetched=1,
    )
    checkpoint = database.resume_checkpoint("run-timeout", DAY_1, DAY_1 + DAY)
    assert checkpoint["checkpoint"] == "opaque-page-2"
    assert checkpoint["status"] == "in_progress"
    assert database.query_usage(DAY_1, DAY_1 + DAY) == []

    database.record_slice_checkpoint(
        "run-timeout",
        DAY_1,
        DAY_1 + DAY,
        checkpoint="opaque-page-2",
        pages_fetched=1,
        status="failed",
        error_code="upstream_timeout",
    )
    database.finish_collection_run("run-timeout", "failed", error_code="upstream_timeout")
    run = database.get_collection_run("run-timeout")
    assert run["status"] == "failed"
    assert run["pages_fetched"] == 1
    assert run["error_code"] == "upstream_timeout"
    assert "opaque-page" not in str(run)


def test_error_code_cannot_store_a_secret_or_response_body(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    database.start_collection_run(DAY_1, DAY_1 + DAY, run_id="run-unsafe")
    with pytest.raises(ValueError, match="sanitized"):
        database.record_slice_checkpoint(
            "run-unsafe",
            DAY_1,
            DAY_1 + DAY,
            checkpoint=None,
            pages_fetched=0,
            status="failed",
            error_code="sk-admin-secret upstream body",
        )


def test_completed_run_requires_gap_free_complete_slice_coverage(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    database.start_collection_run(DAY_1, DAY_1 + DAY, run_id="run-gap")
    database.reconcile_slice(
        "run-gap", DAY_1, DAY_1 + 3600, [make_bucket()], pages_fetched=1
    )
    with pytest.raises(ValueError, match="gap-free"):
        database.finish_collection_run("run-gap", "completed")


def test_daily_and_project_queries_use_utc_boundaries_filters_and_completeness(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    buckets = [
        make_bucket(project_id="proj_alpha1234", project_name="Alpha", input_tokens=100),
        make_bucket(
            project_id="proj_beta5678",
            project_name="Beta",
            model="gpt-5.4",
            input_tokens=200,
            output_tokens=20,
        ),
        make_bucket(
            project_id=None,
            project_name=None,
            model="unknown-model",
            tier="default",
            input_tokens=30,
            output_tokens=3,
        ),
    ]
    reconcile_day(database, buckets)

    projects = database.list_projects()
    assert {project["display_name"] for project in projects} == {"Alpha", "Beta", "Unattributed"}
    alpha_key = PROJECT_KEYS.derive("proj_alpha1234")
    alpha = database.daily_usage(DAY_1, DAY_1 + 2 * DAY, project_key=alpha_key, catalog=CATALOG)
    assert len(alpha) == 1
    assert alpha[0]["day"] == "2026-01-01"
    assert alpha[0]["groups"] == {"standard": 0, "mini": 110}
    assert alpha[0]["other_tokens"] == 0
    assert alpha[0]["completeness"] == "complete"

    all_projects = database.daily_usage(DAY_1, DAY_1 + DAY, catalog=CATALOG)
    assert all_projects[0]["groups"] == {"standard": 220, "mini": 110}
    assert all_projects[0]["other_tokens"] == 33
    assert all_projects[0]["total_tokens"] == 363
    missing = database.daily_usage(
        DAY_1, DAY_1 + 2 * DAY, catalog=CATALOG, include_missing=True
    )
    assert missing[1]["completeness"] == "missing"
    assert missing[1]["total_tokens"] is None


def test_incomplete_empty_slice_is_not_reported_as_zero_usage(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    database.start_collection_run(DAY_1, DAY_1 + DAY, run_id="run-partial")
    database.record_slice_checkpoint(
        "run-partial",
        DAY_1,
        DAY_1 + DAY,
        checkpoint="page-2",
        pages_fetched=1,
    )
    daily = database.daily_usage(DAY_1, DAY_1 + DAY, catalog=CATALOG)
    assert daily[0]["completeness"] == "partial"
    assert daily[0]["total_tokens"] is None


def test_queries_enforce_bounded_ranges(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    with pytest.raises(ValueError, match="cannot exceed"):
        database.query_usage(DAY_1, DAY_1 + (MAX_QUERY_DAYS + 1) * DAY)
    with pytest.raises(ValueError, match="00:00:00Z"):
        database.daily_usage(DAY_1 + 1, DAY_1 + DAY)


def test_integrity_failure_preserves_original_and_blocks_writes(tmp_path):
    path = tmp_path / "damaged.sqlite3"
    original = b"not a sqlite database\x00with evidence"
    path.write_bytes(original)

    database = DatabaseService(path)
    assert database.is_read_only is True
    result = database.check_integrity(full=True)
    assert result.ok is False
    assert result.read_only is True
    assert "restore" in result.guidance
    with pytest.raises(DatabaseReadOnlyError, match="read-only"):
        database.start_collection_run(DAY_1, DAY_1 + DAY)
    assert path.read_bytes() == original


def test_sqlite_backup_api_creates_consistent_atomic_snapshot(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    reconcile_day(database, [make_bucket()])
    backup_path = database.backup_to(tmp_path / "backups" / "history.backup.sqlite3")

    backup = DatabaseService(backup_path)
    assert backup.check_integrity(full=True).ok is True
    assert backup.query_usage(DAY_1, DAY_1 + DAY)[0]["input_tokens"] == 100
    assert not list(backup_path.parent.glob("*.tmp"))


def test_365_day_100_project_synthetic_query_performance(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    buckets = []
    for day_offset in range(365):
        start = DAY_1 + day_offset * DAY
        for project_number in range(100):
            buckets.append(
                make_bucket(
                    start=start,
                    project_id=f"proj_{project_number:03d}",
                    project_name=f"Project {project_number:03d}",
                    input_tokens=project_number + 1,
                    cached_tokens=0,
                )
            )
    database.start_collection_run(DAY_1, DAY_1 + 365 * DAY, run_id="run-365-days")
    database.reconcile_slice(
        "run-365-days",
        DAY_1,
        DAY_1 + 365 * DAY,
        buckets,
        pages_fetched=365,
    )
    database.finish_collection_run("run-365-days", "completed")

    started = time.perf_counter()
    recent = database.query_usage(DAY_1 + 335 * DAY, DAY_1 + 365 * DAY)
    recent_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    one_project = database.query_usage(
        DAY_1,
        DAY_1 + 365 * DAY,
        project_key=PROJECT_KEYS.derive("proj_050"),
    )
    project_elapsed = time.perf_counter() - started
    assert len(recent) == 3000
    assert len(one_project) == 365
    assert recent_elapsed < 1.0
    assert project_elapsed < 3.0
