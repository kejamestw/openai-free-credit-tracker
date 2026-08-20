import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import quota_monitor.export_service as export_module
from quota_monitor.database import DatabaseService, UsageBucket
from quota_monitor.export_service import (
    CSV_COLUMNS,
    atomic_write,
    build_export_records,
    csv_safe_text,
    export_csv,
    export_json,
    render_csv,
)
from quota_monitor.upstream_adapter import ProjectKeyDeriver


DAY = 86_400
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
STAMP = "2026-01-02T03:04:05Z"
FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_KEYS = ProjectKeyDeriver(b"0123456789abcdef")


def bucket(
    *,
    project_id="proj_secretABC123",
    project_name="=SUM(1,1) 專案",
    model="gpt-5.4-mini-2026-03-17",
    input_tokens=100,
):
    return UsageBucket(
        bucket_start_utc=START,
        bucket_end_utc=START + 3600,
        project_id=project_id,
        project_name=project_name,
        model=model,
        service_tier="incentivized-tier",
        input_tokens=input_tokens,
        cached_input_tokens=20,
        output_tokens=10,
        request_count=2,
        catalog_version="catalog-v1",
        collected_at=STAMP,
        project_key=PROJECT_KEYS.derive(project_id),
    )


def populated_database(tmp_path, buckets=None):
    database = DatabaseService(tmp_path / "history.sqlite3")
    database.start_collection_run(START, START + DAY, run_id="run-export", started_at=STAMP)
    database.reconcile_slice(
        "run-export", START, START + DAY, buckets or [bucket()], pages_fetched=1
    )
    database.finish_collection_run("run-export", "completed", finished_at=STAMP)
    return database


@pytest.mark.parametrize(
    "dangerous",
    ["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", "  =hidden", "\t=hidden", "\rformula"],
)
def test_csv_formula_injection_is_neutralized(dangerous):
    assert csv_safe_text(dangerous) == "'" + dangerous
    assert csv_safe_text("ordinary") == "ordinary"


def test_csv_schema_v1_matches_golden_and_is_utf8_parseable(tmp_path):
    database = populated_database(tmp_path)
    path = export_csv(database, tmp_path / "usage.csv", START, START + DAY)
    content = path.read_bytes()

    golden_text = (FIXTURES / "export_schema_v1.csv").read_text(encoding="utf-8")
    assert content == golden_text.replace("\n", "\r\n").encode("utf-8")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[0]["project_name"] == "'=SUM(1,1) 專案"
    assert rows[0]["project_id"] == "proj_…C123"
    assert "proj_secretABC123" not in content.decode("utf-8")
    assert content.endswith(b"\r\n")


def test_json_schema_v1_matches_golden_and_preserves_unicode(tmp_path):
    database = populated_database(tmp_path)
    path = export_json(
        database,
        tmp_path / "usage.json",
        START,
        START + DAY,
        generated_at="2026-01-03T00:00:00Z",
    )
    content = path.read_bytes()

    assert content == (FIXTURES / "export_schema_v1.json").read_bytes()
    payload = json.loads(content)
    assert payload["schema_version"] == 1
    assert payload["time_zone"] == "UTC"
    assert payload["filters"]["project_id_policy"] == "mask"
    assert payload["records"][0]["project_name"] == "=SUM(1,1) 專案"
    assert "proj_secretABC123" not in content.decode("utf-8")


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("mask", "proj_…C123"),
        ("exclude", ""),
        ("include", "proj_secretABC123"),
    ],
)
def test_project_id_export_policy_is_explicit_and_defaults_to_mask(tmp_path, policy, expected):
    database = populated_database(tmp_path)
    records = build_export_records(
        database, START, START + DAY, project_id_policy=policy
    )
    assert records[0]["project_id"] == expected

    default_records = build_export_records(database, START, START + DAY)
    assert default_records[0]["project_id"] == "proj_…C123"


def test_export_uses_full_database_rows_and_same_project_filter(tmp_path):
    database = populated_database(
        tmp_path,
        [
            bucket(),
            bucket(
                project_id="proj_beta9876",
                project_name="Beta 專案",
                model="gpt-5.4",
                input_tokens=250,
            ),
        ],
    )
    beta_key = PROJECT_KEYS.derive("proj_beta9876")
    database_rows = database.query_usage(START, START + DAY, project_key=beta_key)
    records = build_export_records(
        database,
        START,
        START + DAY,
        project_key=beta_key,
        project_id_policy="exclude",
    )

    assert len(records) == len(database_rows) == 1
    assert records[0]["project_key"] == beta_key
    assert records[0]["input_tokens"] == database_rows[0]["input_tokens"] == 250
    assert records[0]["total_tokens"] == database_rows[0]["total_tokens"] == 260
    assert records[0]["project_id"] == ""


def test_empty_csv_still_has_fixed_schema_header():
    content = render_csv([]).decode("utf-8")
    assert content == ",".join(CSV_COLUMNS) + "\r\n"


def test_atomic_write_failure_keeps_previous_destination_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    destination = tmp_path / "usage.json"
    destination.write_bytes(b"previous complete export")

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(export_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_write(destination, b"new export")

    assert destination.read_bytes() == b"previous complete export"
    assert list(tmp_path.glob(".usage.json.*.tmp")) == []


def test_invalid_project_id_policy_fails_closed(tmp_path):
    database = populated_database(tmp_path)
    with pytest.raises(ValueError, match="mask, exclude, or include"):
        build_export_records(
            database,
            START,
            START + DAY,
            project_id_policy="surprise",  # type: ignore[arg-type]
        )
