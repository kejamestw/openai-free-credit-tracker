from datetime import datetime, timedelta, timezone

import pytest

from quota_monitor.database import DEFAULT_PROFILE_ID, DatabaseService, UsageBucket
from quota_monitor.openai_client import OpenAIClientError
from quota_monitor.sync_service import (
    DEFAULT_SYNC_DAYS,
    HistoryOperations,
    UsageSyncService,
    default_sync_range,
    utc_day_slices,
)
from quota_monitor.upstream_adapter import ProjectKeyDeriver


DAY = 86_400
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
PROJECT_SECRET = b"0123456789abcdef"
RAW_PROJECT_ID = "project-private-a"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def api_bucket(start, *, input_tokens=100, project_id=RAW_PROJECT_ID, model="gpt-5.6-terra"):
    return {
        "start_time": start,
        "end_time": start + DAY,
        "results": [
            {
                "project_id": project_id,
                "model": model,
                "service_tier": "incentivized-tier",
                "input_tokens": input_tokens,
                "input_cached_tokens": 20,
                "output_tokens": 5,
                "num_model_requests": 2,
            }
        ],
    }


def service(database, *, profile_id=DEFAULT_PROFILE_ID):
    return UsageSyncService(
        database,
        project_keys=ProjectKeyDeriver(PROJECT_SECRET),
        catalog_version="catalog-v1",
        profile_id=profile_id,
    )


def test_default_range_is_last_30_complete_utc_days_and_stable_during_day():
    taipei = timezone(timedelta(hours=8))
    morning = datetime(2026, 2, 1, 8, 1, tzinfo=taipei)
    evening = datetime(2026, 2, 1, 23, 59, tzinfo=taipei)

    morning_range = default_sync_range(morning)
    evening_range = default_sync_range(evening)
    assert morning_range == evening_range
    assert morning_range[1] == int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    assert morning_range[1] - morning_range[0] == DEFAULT_SYNC_DAYS * DAY
    assert len(utc_day_slices(*morning_range)) == 30


def test_utc_slices_split_unaligned_range_at_midnight():
    assert utc_day_slices(START + 3600, START + 2 * DAY + 7200) == (
        (START + 3600, START + DAY),
        (START + DAY, START + 2 * DAY),
        (START + 2 * DAY, START + 2 * DAY + 7200),
    )


def test_sync_maps_paginated_records_reconciles_each_day_and_reports_safe_progress(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    client = FakeClient(
        [
            {"data": [api_bucket(START, input_tokens=100)], "next_page": "page-2"},
            {"data": [api_bucket(START, input_tokens=50)], "next_page": None},
            {"data": [api_bucket(START + DAY, project_id=None)], "next_page": None},
        ]
    )
    events = []

    result = service(database).sync(
        client,
        start_utc=START,
        end_utc=START + 2 * DAY,
        progress=events.append,
    )

    assert result.status == "completed"
    assert (result.completed_slices, result.total_slices, result.pages_fetched) == (2, 2, 3)
    rows = database.query_usage(START, START + 2 * DAY)
    assert len(rows) == 2
    attributed = next(row for row in rows if row["project_id"])
    assert attributed["project_key"] == ProjectKeyDeriver(PROJECT_SECRET).derive(RAW_PROJECT_ID)
    assert attributed["project_id"] == RAW_PROJECT_ID
    assert attributed["input_tokens"] == 150
    assert next(row for row in rows if row["project_id"] is None)["project_key"] == "unattributed"
    assert [event.event for event in events].count("page_fetched") == 3
    assert events[-1].event == "run_completed"
    assert RAW_PROJECT_ID not in repr(events)
    assert RAW_PROJECT_ID not in repr(result)


def test_partial_failure_commits_only_complete_days_and_resume_restarts_failed_slice(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    synchronizer = service(database)
    first_client = FakeClient(
        [
            {"data": [api_bucket(START)], "next_page": None},
            {"data": [api_bucket(START + DAY, input_tokens=200)], "next_page": "page-2"},
            OpenAIClientError("upstream_timeout", "safe timeout", 504, True),
        ]
    )

    first = synchronizer.sync(first_client, start_utc=START, end_utc=START + 2 * DAY)
    assert first.status == "partial"
    assert len(database.query_usage(START, START + 2 * DAY)) == 1
    slices = database.list_collection_slices(first.run_id)
    assert [item["status"] for item in slices] == ["completed", "failed"]
    assert slices[1]["checkpoint"] == "page-2"
    assert slices[1]["pages_fetched"] == 1

    resume_client = FakeClient(
        [
            {"data": [api_bucket(START + DAY, input_tokens=225)], "next_page": "page-2"},
            {"data": [api_bucket(START + DAY, input_tokens=25)], "next_page": None},
        ]
    )
    events = []
    resumed = synchronizer.sync(
        resume_client,
        start_utc=START,
        end_utc=START + 2 * DAY,
        progress=events.append,
    )

    assert resumed.status == "completed"
    assert resumed.resumed is True
    assert resumed.run_id == first.run_id
    assert [params.get("page") for _, params in resume_client.calls] == [None, "page-2"]
    assert any(event.event == "slice_skipped" for event in events)
    rows = database.query_usage(START, START + 2 * DAY)
    assert len(rows) == 2
    second_day = next(row for row in rows if row["bucket_start_utc"] == START + DAY)
    assert second_day["input_tokens"] == 250


def test_failure_does_not_persist_partial_page_project_or_leak_it_in_progress(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    client = FakeClient(
        [
            {"data": [api_bucket(START)], "next_page": "page-2"},
            RuntimeError(f"upstream body contained {RAW_PROJECT_ID}"),
        ]
    )
    events = []

    result = service(database).sync(
        client, start_utc=START, end_utc=START + DAY, progress=events.append
    )

    assert result.status == "failed"
    assert result.error_code == "sync_failed"
    assert database.query_usage(START, START + DAY) == []
    assert database.list_projects() == []
    assert RAW_PROJECT_ID not in repr(events)
    assert RAW_PROJECT_ID not in repr(result)
    assert RAW_PROJECT_ID not in repr(database.get_collection_run(result.run_id))


def test_unsafe_upstream_error_code_is_replaced_before_persistence(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    client = FakeClient(
        [OpenAIClientError("sk-admin-secret response body", "unsafe", 502)]
    )

    result = service(database).sync(client, start_utc=START, end_utc=START + DAY)

    assert result.error_code == "sync_failed"
    assert database.get_collection_run(result.run_id)["error_code"] == "sync_failed"
    assert "sk-admin" not in repr(result)


def test_same_range_three_times_is_idempotent_and_third_run_updates_correction(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    synchronizer = service(database)
    inputs = (100, 100, 175)
    for value in inputs:
        result = synchronizer.sync(
            FakeClient([{"data": [api_bucket(START, input_tokens=value)], "next_page": None}]),
            start_utc=START,
            end_utc=START + DAY,
        )
        assert result.status == "completed"

    rows = database.query_usage(START, START + DAY)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 175
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 3


def test_sync_writes_only_to_its_selected_profile(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    profile_id = "prof_" + "c" * 32
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO profiles VALUES (?, 'Scoped', 'cred-scoped', NULL, 1, ?, NULL)",
            (profile_id, "2026-01-01T00:00:00Z"),
        )

    result = service(database, profile_id=profile_id).sync(
        FakeClient([{"data": [api_bucket(START)], "next_page": None}]),
        start_utc=START,
        end_utc=START + DAY,
    )

    assert result.profile_id == profile_id
    assert database.query_usage(START, START + DAY) == []
    assert len(database.query_usage(START, START + DAY, profile_id=profile_id)) == 1


def stored_bucket(start, *, input_tokens=100):
    return UsageBucket(
        bucket_start_utc=start,
        bucket_end_utc=start + DAY,
        project_id="proj_retention",
        project_name="Retention",
        model="gpt-5.6-terra",
        service_tier="incentivized-tier",
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=1,
        request_count=1,
        catalog_version="catalog-v1",
        collected_at="2026-01-20T00:00:00Z",
        project_key=ProjectKeyDeriver(PROJECT_SECRET).derive("proj_retention"),
    )


def persist_day(database, start, run_id):
    database.start_collection_run(start, start + DAY, run_id=run_id)
    database.reconcile_slice(run_id, start, start + DAY, [stored_bucket(start)], pages_fetched=1)
    database.finish_collection_run(run_id, "completed")


def test_retention_requires_preview_and_confirmation_and_keeps_recent_history(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    old = int(datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp())
    recent = int(datetime(2026, 1, 18, tzinfo=timezone.utc).timestamp())
    persist_day(database, old, "old")
    persist_day(database, recent, "recent")
    operations = HistoryOperations(database)
    preview = operations.preview_retention(
        5, now=datetime(2026, 1, 20, 12, tzinfo=timezone.utc)
    )

    assert preview.cutoff_utc == int(datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp())
    assert preview.row_count == 1
    with pytest.raises(ValueError, match="confirmation"):
        operations.apply_retention(preview)
    result = operations.apply_retention(preview, confirm=True)
    assert result.deleted_rows == 1
    assert database.query_usage(recent, recent + DAY)[0]["input_tokens"] == 100


def test_retention_rejects_stale_preview(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    old = int(datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp())
    operations = HistoryOperations(database)
    preview = operations.preview_retention(
        5, now=datetime(2026, 1, 20, 12, tzinfo=timezone.utc)
    )
    persist_day(database, old, "old-after-preview")
    with pytest.raises(ValueError, match="changed after preview"):
        operations.apply_retention(preview, confirm=True)
    assert len(database.query_usage(old, old + DAY)) == 1


def test_history_operations_expose_integrity_and_consistent_backup(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    persist_day(database, START, "backup-source")
    operations = HistoryOperations(database)

    assert operations.check_integrity().ok is True
    backup_path = operations.create_backup(tmp_path / "backup" / "history.sqlite3")
    backup = DatabaseService(backup_path)
    assert backup.check_integrity(full=True).ok is True
    assert backup.query_usage(START, START + DAY)[0]["project_key"] == ProjectKeyDeriver(
        PROJECT_SECRET
    ).derive("proj_retention")
