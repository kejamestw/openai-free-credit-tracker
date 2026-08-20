import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from quota_monitor.config_service import config_to_dict, parse_config
from quota_monitor.database import DEFAULT_PROFILE_ID, DatabaseService


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "upgrade"
MATRIX_PATH = FIXTURE_ROOT / "matrix.json"
START = 1_767_225_600
END = 1_777_680_001
V05_PROFILE_ID = "prof_22222222222222222222222222222222"


def _matrix_entries():
    document = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    return document["entries"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _materialize_sql_fixture(source: Path, destination: Path) -> None:
    connection = sqlite3.connect(destination)
    try:
        connection.executescript(source.read_text(encoding="utf-8"))
    finally:
        connection.close()


@pytest.mark.parametrize("entry", _matrix_entries(), ids=lambda item: item["contract_id"])
def test_unpublished_upgrade_contracts_are_consumable_and_immutable(tmp_path, entry):
    config_path = FIXTURE_ROOT / entry["config_fixture"]
    database_path = (FIXTURE_ROOT / entry["database_fixture"]).resolve()
    source_paths = (MATRIX_PATH, config_path, database_path)
    original_digests = {path: _digest(path) for path in source_paths}

    config_document = json.loads(config_path.read_text(encoding="utf-8"))
    config = parse_config(config_document)
    assert config.ui.language == entry["expected_language"]
    assert config.network.request_timeout_seconds == entry["expected_timeout_seconds"]
    assert config_to_dict(config)["schema_version"] == 1

    working_database = tmp_path / f"{entry['contract_id']}.sqlite3"
    _materialize_sql_fixture(database_path, working_database)
    database = DatabaseService(working_database)

    assert database.schema_version == 2
    profile_id = V05_PROFILE_ID if entry["database_schema"] == 2 else DEFAULT_PROFILE_ID
    rows = database.query_usage(START, END, profile_id=profile_id)
    assert sum(row["input_tokens"] for row in rows) == entry["expected_input_tokens"]
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    if entry["database_schema"] == 1:
        assert database.last_migration_backup is not None
    else:
        assert database.last_migration_backup is None
    assert {path: _digest(path) for path in source_paths} == original_digests


def test_v03_and_v04_explicitly_alias_the_same_schema_v1_lineage():
    entries = {entry["contract_id"]: entry for entry in _matrix_entries()}
    v03 = entries["v0.3-contract"]
    v04 = entries["v0.4-contract"]

    assert v03["database_fixture"] == v04["database_fixture"]
    assert v03["database_schema"] == v04["database_schema"] == 1
    assert "shared" in v03["database_lineage"]
    assert "shared" in v04["database_lineage"]


def test_upgrade_matrix_does_not_claim_published_release_tags():
    document = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    assert document["fixture_contract"] == "unpublished-pre-v1-upgrade-matrix"
    assert "not claims of released tags" in document["publication_note"]
    assert {entry["contract_id"] for entry in document["entries"]} == {
        "v0.2-contract",
        "v0.3-contract",
        "v0.4-contract",
        "v0.5-contract",
    }
