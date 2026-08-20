import errno
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import quota_monitor.database as database_module
from quota_monitor.database import (
    DEFAULT_PROFILE_ID,
    DatabaseService,
    MigrationError,
    UsageBucket,
)
from quota_monitor.export_service import build_export_records
from quota_monitor.upstream_adapter import ProjectKeyDeriver


FIXTURE = Path(__file__).parent / "fixtures" / "schema_v1_snapshot.sql"
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
DAY = 86_400
PROFILE_A = "prof_" + "a" * 32
PROFILE_B = "prof_" + "b" * 32
PROJECT_ID = "proj_same_private"
PROJECT_KEY = ProjectKeyDeriver(b"0123456789abcdef").derive(PROJECT_ID)


def create_v1_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
    finally:
        connection.close()


def assert_database_remains_v1(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM usage_buckets").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'profiles'"
        ).fetchone() is None
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def insert_profile(database, profile_id, credential_id):
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO profiles VALUES (?, ?, ?, NULL, 1, ?, NULL)
            """,
            (profile_id, profile_id[-4:], credential_id, "2026-01-01T00:00:00Z"),
        )


def scoped_bucket(tokens):
    return UsageBucket(
        START,
        START + 3600,
        PROJECT_ID,
        "Same project",
        "gpt-5.6-terra",
        "incentivized-tier",
        tokens,
        0,
        5,
        1,
        "catalog-v1",
        "2026-01-02T00:00:00Z",
        PROJECT_KEY,
    )


def reconcile_profile(database, profile_id, tokens):
    database.start_collection_run(
        START, START + DAY, run_id="same-run", profile_id=profile_id
    )
    database.reconcile_slice(
        "same-run",
        START,
        START + DAY,
        [scoped_bucket(tokens)],
        pages_fetched=1,
        profile_id=profile_id,
    )
    database.finish_collection_run(
        "same-run", "completed", profile_id=profile_id
    )


def test_golden_v1_snapshot_migrates_to_default_profile_with_verified_backup(tmp_path):
    path = tmp_path / "history.sqlite3"
    create_v1_snapshot(path)

    database = DatabaseService(path)

    assert database.schema_version == 2
    assert database.query_usage(START, START + DAY)[0]["profile_id"] == DEFAULT_PROFILE_ID
    assert database.get_collection_run("legacy-run")["profile_id"] == DEFAULT_PROFILE_ID
    with database.connection() as connection:
        profile = connection.execute(
            "SELECT * FROM profiles WHERE profile_id = ?", (DEFAULT_PROFILE_ID,)
        ).fetchone()
    assert profile["credential_id"] == "cred_default_migration_pending"

    backup_path, metadata_path = database.last_migration_backup
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["schema_from"] == 1
    assert metadata["schema_to"] == 2
    assert metadata["backup_file"] == backup_path.name
    assert metadata["sha256"] == hashlib.sha256(backup_path.read_bytes()).hexdigest()

    restored_path = DatabaseService.restore_from_backup(
        backup_path, tmp_path / "restored-v1.sqlite3"
    )
    restored = sqlite3.connect(restored_path)
    try:
        assert restored.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM usage_buckets").fetchone()[0] == 1
    finally:
        restored.close()

    migrated_restore = DatabaseService(restored_path)
    assert migrated_restore.schema_version == 2
    assert migrated_restore.query_usage(START, START + DAY)[0]["input_tokens"] == 100


def test_v1_migration_failure_rolls_back_and_retry_is_safe(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    create_v1_snapshot(path)
    original = database_module._MIGRATIONS[2]
    monkeypatch.setitem(
        database_module._MIGRATIONS,
        2,
        original[:6] + ("THIS IS NOT SQL",) + original[6:],
    )

    with pytest.raises(MigrationError):
        DatabaseService(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM usage_buckets").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'profiles'"
        ).fetchone() is None
    finally:
        connection.close()
    assert list(tmp_path.glob("*.schema-v1-to-v2.*.backup"))
    assert list(tmp_path.glob("*.backup.metadata.json"))

    monkeypatch.setitem(database_module._MIGRATIONS, 2, original)
    database = DatabaseService(path)
    assert database.schema_version == 2
    assert len(database.query_usage(START, START + DAY)) == 1
    assert DatabaseService(path).last_migration_backup is None


def test_interrupted_migration_rolls_back_and_retry_is_safe(tmp_path):
    path = tmp_path / "history.sqlite3"
    fixture_bytes = FIXTURE.read_bytes()
    create_v1_snapshot(path)

    def interrupt_after_backup(phase, version, statement_index):
        if phase == "before-statement" and version == 2 and statement_index == 2:
            raise KeyboardInterrupt("simulated process interruption")

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        DatabaseService(path, migration_checkpoint=interrupt_after_backup)

    assert_database_remains_v1(path)
    assert list(tmp_path.glob("*.schema-v1-to-v2.*.backup"))
    assert DatabaseService(path).schema_version == 2
    assert FIXTURE.read_bytes() == fixture_bytes


def test_backup_failure_prevents_migration_and_retry_is_safe(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    fixture_bytes = FIXTURE.read_bytes()
    create_v1_snapshot(path)
    original = DatabaseService.backup_to

    def fail_backup(_database, _destination):
        raise OSError(errno.EACCES, "simulated backup permission failure")

    monkeypatch.setattr(DatabaseService, "backup_to", fail_backup)
    with pytest.raises(MigrationError) as caught:
        DatabaseService(path)

    assert isinstance(caught.value.__cause__, OSError)
    assert_database_remains_v1(path)
    monkeypatch.setattr(DatabaseService, "backup_to", original)
    assert DatabaseService(path).schema_version == 2
    assert FIXTURE.read_bytes() == fixture_bytes


def test_disk_full_during_backup_metadata_keeps_v1_and_allows_retry(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    fixture_bytes = FIXTURE.read_bytes()
    create_v1_snapshot(path)
    original = database_module._atomic_bytes_write

    def fail_metadata_write(_path, _content):
        raise OSError(errno.ENOSPC, "simulated disk full")

    monkeypatch.setattr(database_module, "_atomic_bytes_write", fail_metadata_write)
    with pytest.raises(MigrationError) as caught:
        DatabaseService(path)

    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.ENOSPC
    assert_database_remains_v1(path)
    assert list(tmp_path.glob("*.schema-v1-to-v2.*.backup"))
    assert not list(tmp_path.glob("*.backup.metadata.json"))
    monkeypatch.setattr(database_module, "_atomic_bytes_write", original)
    assert DatabaseService(path).schema_version == 2
    assert FIXTURE.read_bytes() == fixture_bytes


def test_readonly_database_failure_keeps_v1_and_allows_retry(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    fixture_bytes = FIXTURE.read_bytes()
    create_v1_snapshot(path)
    original = DatabaseService._connect

    def readonly_connect(database, *, write):
        if write:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return original(database, write=write)

    monkeypatch.setattr(DatabaseService, "_connect", readonly_connect)
    with pytest.raises(MigrationError) as caught:
        DatabaseService(path)

    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert_database_remains_v1(path)
    monkeypatch.setattr(DatabaseService, "_connect", original)
    assert DatabaseService(path).schema_version == 2
    assert FIXTURE.read_bytes() == fixture_bytes


def test_busy_database_is_retryable_and_not_classified_as_corrupt(tmp_path):
    path = tmp_path / "history.sqlite3"
    fixture_bytes = FIXTURE.read_bytes()
    create_v1_snapshot(path)
    blocker = sqlite3.connect(path)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(MigrationError) as caught:
            DatabaseService(path, busy_timeout_ms=10)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert "locked" in str(caught.value.__cause__).lower()
    finally:
        blocker.rollback()
        blocker.close()

    assert_database_remains_v1(path)
    database = DatabaseService(path, busy_timeout_ms=100)
    assert database.schema_version == 2
    assert not database.is_read_only
    assert FIXTURE.read_bytes() == fixture_bytes


def test_identical_projects_buckets_and_run_ids_are_isolated_across_profiles(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    insert_profile(database, PROFILE_A, "cred-a")
    insert_profile(database, PROFILE_B, "cred-b")

    reconcile_profile(database, PROFILE_A, 100)
    reconcile_profile(database, PROFILE_B, 900)

    rows_a = database.query_usage(START, START + DAY, profile_id=PROFILE_A)
    rows_b = database.query_usage(START, START + DAY, profile_id=PROFILE_B)
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0]["input_tokens"] == 100
    assert rows_b[0]["input_tokens"] == 900
    assert database.get_collection_run("same-run", profile_id=PROFILE_A)["pages_fetched"] == 1
    assert database.get_collection_run("same-run", profile_id=PROFILE_B)["pages_fetched"] == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_buckets").fetchone()[0] == 2


def test_export_records_include_selected_profile_and_never_mix_profiles(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    insert_profile(database, PROFILE_A, "cred-a")
    insert_profile(database, PROFILE_B, "cred-b")
    reconcile_profile(database, PROFILE_A, 100)
    reconcile_profile(database, PROFILE_B, 900)

    records = build_export_records(
        database, START, START + DAY, profile_id=PROFILE_B
    )

    assert len(records) == 1
    assert records[0]["profile_id"] == PROFILE_B
    assert records[0]["input_tokens"] == 900
