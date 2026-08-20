import json

from quota_monitor.app import build_parser
from quota_monitor.config_service import AppConfig, ConfigService, ProfilesSettings
from quota_monitor.database import DEFAULT_PROFILE_ID, DatabaseService, UsageBucket
from quota_monitor.operations_cli import RUNTIME_LOCK_FILENAME, run_operation
from quota_monitor.platform_adapters import FileInstanceLock
from quota_monitor.platform_paths import AppPaths
from quota_monitor.upstream_adapter import ProjectKeyDeriver


DAY = 1_728_000_000
RAW_PROJECT_ID = "project-private-cli-123456"


def make_paths(tmp_path):
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )
    paths.ensure_directories()
    return paths


def seed(database, start=DAY):
    project_key = ProjectKeyDeriver(b"cli-project-key-material-32-bytes!").derive(
        RAW_PROJECT_ID
    )
    run_id = database.start_collection_run(start, start + 86_400)
    database.reconcile_slice(
        run_id,
        start,
        start + 86_400,
        (
            UsageBucket(
                start,
                start + 86_400,
                RAW_PROJECT_ID,
                None,
                "gpt-4o-mini",
                "priority",
                4,
                0,
                2,
                1,
                "catalog-test",
                "2026-01-01T00:00:00Z",
                project_key,
            ),
        ),
        pages_fetched=1,
    )
    database.finish_collection_run(run_id, "completed")


def test_operations_cli_backup_integrity_and_safe_export(tmp_path, capsys):
    paths = make_paths(tmp_path)
    database = DatabaseService(paths.data_dir / "history.sqlite3")
    seed(database)
    ConfigService(paths).save(
        AppConfig(profiles=ProfilesSettings(active_profile_id=DEFAULT_PROFILE_ID))
    )
    parser = build_parser()

    backup = tmp_path / "manual.sqlite3"
    assert run_operation(parser.parse_args(["backup", "--output", str(backup)]), paths) == 0
    assert backup.is_file()

    assert run_operation(parser.parse_args(["integrity", "--full"]), paths) == 0
    integrity_line = capsys.readouterr().out.splitlines()[-1]
    assert json.loads(integrity_line)["ok"] is True

    destination = tmp_path / "usage.json"
    args = parser.parse_args(
        [
            "export",
            "--output",
            str(destination),
            "--format",
            "json",
            "--start-utc",
            str(DAY),
            "--end-utc",
            str(DAY + 86_400),
        ]
    )
    assert run_operation(args, paths) == 0
    content = destination.read_text(encoding="utf-8")
    assert RAW_PROJECT_ID not in content
    assert json.loads(content)["filters"]["profile_id"] == DEFAULT_PROFILE_ID


def test_restore_requires_confirmation_and_shared_offline_lock(tmp_path, capsys):
    paths = make_paths(tmp_path)
    parser = build_parser()
    source = tmp_path / "backup.sqlite3"

    args = parser.parse_args(["restore", "--source", str(source)])
    assert run_operation(args, paths) == 2
    assert "--confirm-restore" in capsys.readouterr().err

    owner = FileInstanceLock((paths.data_dir / RUNTIME_LOCK_FILENAME).resolve())
    assert owner.acquire()
    try:
        args = parser.parse_args(
            ["restore", "--source", str(source), "--confirm-restore"]
        )
        assert run_operation(args, paths) == 3
        assert "stopped" in capsys.readouterr().err
    finally:
        owner.release()


def test_database_replace_from_backup_restores_verified_snapshot(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    seed(database)
    backup = database.backup_to(tmp_path / "backup.sqlite3")
    seed(database, DAY + 86_400)
    assert len(database.query_usage(DAY, DAY + 172_800)) == 2

    database.replace_from_backup(backup)

    assert len(database.query_usage(DAY, DAY + 172_800)) == 1
    assert database.check_integrity(full=True).ok is True
