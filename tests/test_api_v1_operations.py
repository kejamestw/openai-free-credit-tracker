import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

from quota_monitor.config_service import ConfigService
from quota_monitor.database import DatabaseService, UsageBucket
from quota_monitor.model_catalog import load_catalog
from quota_monitor.platform_adapters import (
    InMemoryCredentialStore,
    InMemoryNotificationAdapter,
)
from quota_monitor.platform_paths import AppPaths
from quota_monitor.runtime import (
    CredentialVerification,
    OpenAIUsageCredentialVerifier,
    RuntimeRequestError,
    RuntimeServices,
)
from quota_monitor.server import create_server
from quota_monitor.upstream_adapter import ProjectKeyDeriver


ADMIN_KEY = "sk-" + "admin-contract-secret-12345678"
RAW_PROJECT_ID = "project-raw-private-123456"
DAY = 1_728_000_000


def make_paths(tmp_path):
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )
    paths.ensure_directories()
    return paths


class FakeClient:
    def __init__(self, key, *, bucket=None, entered=None, release=None):
        self._key = key
        self._bucket = bucket
        self._entered = entered
        self._release = release

    def __repr__(self):
        return "FakeClient(credential=<redacted>)"

    def get(self, path, _params):
        assert self._key == ADMIN_KEY or self._key.startswith("sk-" + "admin-second-")
        if self._entered is not None and path == "/organization/usage/completions":
            self._entered.set()
            assert self._release.wait(5)
        return {"data": [] if self._bucket is None else [self._bucket], "has_more": False}


def make_runtime(
    tmp_path,
    *,
    client_factory=None,
    credential_verifier=None,
    notification_adapter=None,
):
    paths = make_paths(tmp_path)
    database = DatabaseService(paths.data_dir / "history.sqlite3")
    factory = client_factory or (lambda key, _timeout: FakeClient(key))
    runtime = RuntimeServices(
        paths=paths,
        config_service=ConfigService(paths),
        database=database,
        credential_store=InMemoryCredentialStore(),
        credential_verifier=credential_verifier or OpenAIUsageCredentialVerifier(
            clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc)
        ),
        admin_client_factory=factory,
        project_keys=ProjectKeyDeriver(b"pseudonym-key-material-32-bytes!!"),
        notification_adapter=notification_adapter,
        catalog=load_catalog(),
    )
    return runtime, database


@contextmanager
def running(runtime):
    server = create_server(runtime_services=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, path, *, method="GET", document=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {"Host": f"127.0.0.1:{server.server_port}"}
    body = None
    if document is not None:
        body = json.dumps(document).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    connection.close()
    return result


def create_and_activate(server, key=ADMIN_KEY):
    status, _, body = request(
        server,
        "/api/v1/profiles",
        method="POST",
        document={"display_name": "Primary", "admin_key": key},
    )
    assert status == 201
    profile = json.loads(body)
    status, _, _ = request(
        server,
        f"/api/v1/profiles/{profile['profile_id']}/activate",
        method="POST",
        document={},
    )
    assert status == 200
    return profile["profile_id"]


def seed_usage_day(database, profile_id, project_keys, start=DAY):
    project_key = project_keys.derive(RAW_PROJECT_ID)
    run_id = database.start_collection_run(start, start + 86_400, profile_id=profile_id)
    database.reconcile_slice(
        run_id,
        start,
        start + 86_400,
        (
            UsageBucket(
                start,
                start + 86_400,
                RAW_PROJECT_ID,
                "=unsafe display",
                "gpt-4o-mini",
                "priority",
                10,
                1,
                5,
                2,
                "test-catalog",
                "2026-01-02T00:00:00Z",
                project_key,
            ),
        ),
        pages_fetched=1,
        profile_id=profile_id,
    )
    database.finish_collection_run(run_id, "completed", profile_id=profile_id)
    return project_key


def seed_usage(database, profile_id, project_keys):
    return seed_usage_day(database, profile_id, project_keys)


def test_profile_api_lifecycle_is_safe_and_profile_aware(tmp_path, capsys):
    runtime, _database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        status, _, body = request(server, "/api/v1/profiles")
        assert status == 200
        payload = json.loads(body)
        selected = next(item for item in payload["profiles"] if item["profile_id"] == profile_id)
        assert selected["active"] is True
        assert selected["credential_configured"] is True
        assert selected["identity_status"] == "capability_verified"

        status, _, body = request(
            server,
            f"/api/v1/profiles/{profile_id}",
            method="PUT",
            document={"display_name": "Renamed", "enabled": True},
        )
        assert status == 200
        assert json.loads(body)["display_name"] == "Renamed"

        status, _, body = request(
            server,
            f"/api/v1/profiles/{profile_id}/credential",
            method="PUT",
            document={"admin_key": ADMIN_KEY},
        )
        assert status == 200
        assert json.loads(body)["identity_status"] == "capability_verified"

        status, _, body = request(
            server,
            "/api/v1/sync",
            method="POST",
            document={
                "profile_id": profile_id,
                "start_utc": DAY,
                "end_utc": DAY + 86_400,
            },
        )
        assert status == 200
        assert json.loads(body)["profile_id"] == profile_id

        status, _, body = request(
            server,
            f"/api/v1/profiles/{profile_id}/credential",
            method="DELETE",
        )
        assert status == 200
        assert json.loads(body)["credential_configured"] is False
        assert json.loads(body)["enabled"] is False

    output = capsys.readouterr().out
    assert ADMIN_KEY not in output
    assert ADMIN_KEY.encode() not in body
    assert b"credential_id" not in body
    assert b"organization_ref" not in body


def test_duplicate_credential_is_blocked_across_profiles_without_fingerprint(tmp_path):
    runtime, _database = make_runtime(tmp_path)
    first = runtime.create_profile(display_name="First", admin_key=ADMIN_KEY)

    with pytest.raises(RuntimeRequestError) as duplicate_create:
        runtime.create_profile(display_name="Duplicate", admin_key=ADMIN_KEY)
    assert duplicate_create.value.code == "duplicate_credential"
    assert len(runtime.list_profiles()["profiles"]) == 2  # includes the migrated default profile

    second_key = "sk-" + "admin-second-secret-12345678"
    second = runtime.create_profile(display_name="Second", admin_key=second_key)
    with pytest.raises(RuntimeRequestError) as duplicate_replace:
        runtime.replace_profile_credential(second["profile_id"], admin_key=ADMIN_KEY)
    assert duplicate_replace.value.code == "duplicate_credential"

    first_profile = runtime.get_profile(first["profile_id"])
    second_profile = runtime.get_profile(second["profile_id"])
    assert first_profile["credential_configured"] is True
    assert second_profile["credential_configured"] is True
    with runtime.database.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
    assert not {"credential_fingerprint", "admin_key", "api_key"} & columns


def test_history_export_alert_backup_and_integrity_contracts(tmp_path):
    runtime, database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        project_key = seed_usage(database, profile_id, runtime.project_keys)

        query = urlencode(
            {"profile_id": profile_id, "start_utc": DAY, "end_utc": DAY + 86_400}
        )
        status, _, body = request(server, f"/api/v1/history?{query}")
        history = json.loads(body)
        assert status == 200
        assert history["profile_id"] == profile_id
        assert history["records"][0]["completeness"] == "complete"

        status, _, body = request(server, f"/api/v1/projects?profile_id={profile_id}")
        projects = json.loads(body)
        assert status == 200
        assert projects["projects"] == [
            {
                "project_key": project_key,
                "display_name": "=unsafe display",
                "bucket_count": 1,
            }
        ]
        assert RAW_PROJECT_ID.encode() not in body

        status, headers, body = request(
            server,
            "/api/v1/export",
            method="POST",
            document={
                "format": "json",
                "profile_id": profile_id,
                "start_utc": DAY,
                "end_utc": DAY + 86_400,
                "project_key": project_key,
                "project_id_policy": "mask",
            },
        )
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert RAW_PROJECT_ID.encode() not in body
        assert json.loads(body)["filters"]["profile_id"] == profile_id

        group_id = next(iter(runtime.catalog["groups"]))
        status, _, body = request(
            server,
            "/api/v1/alerts",
            method="POST",
            document={
                "profile_id": profile_id,
                "group_id": group_id,
                "threshold_percent": 80,
                "project_key": project_key,
            },
        )
        assert status == 201
        rule_id = json.loads(body)["rule_id"]
        status, _, body = request(server, f"/api/v1/alerts?profile_id={profile_id}")
        assert status == 200 and json.loads(body)["rules"][0]["rule_id"] == rule_id
        status, _, body = request(
            server,
            f"/api/v1/alerts/{rule_id}?profile_id={profile_id}",
            method="DELETE",
        )
        assert status == 200 and json.loads(body)["deleted"] is True

        status, _, body = request(
            server, "/api/v1/operations/backup", method="POST", document={}
        )
        assert status == 201
        backup_name = json.loads(body)["backup_name"]
        assert (runtime.paths.data_dir / "backups" / backup_name).is_file()

        status, _, body = request(server, "/api/v1/operations/integrity?full=true")
        assert status == 200 and json.loads(body)["ok"] is True

        status, _, body = request(
            server,
            "/api/v1/operations/restore",
            method="POST",
            document={"backup_name": backup_name, "confirm": False},
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "confirmation_required"

        status, _, body = request(
            server,
            "/api/v1/operations/restore",
            method="POST",
            document={"backup_name": backup_name, "confirm": True},
        )
        assert status == 200
        assert json.loads(body)["restored"] is True


def test_notification_test_is_generic_recorded_and_profile_scoped(tmp_path):
    notifications = InMemoryNotificationAdapter()
    runtime, _database = make_runtime(
        tmp_path, notification_adapter=notifications
    )
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        status, _, body = request(
            server,
            "/api/v1/notifications/test",
            method="POST",
            document={"profile_id": profile_id},
        )
        response = json.loads(body)
        assert status == 200
        assert response["sent"] is True
        assert response["profile_id"] == profile_id

        status, _, body = request(
            server, f"/api/v1/alerts/history?profile_id={profile_id}&limit=10"
        )
        history = json.loads(body)
        assert status == 200
        assert history["records"][0]["event_kind"] == "quota_threshold_test"
        assert history["records"][0]["delivery_status"] == "test"

    assert len(notifications.messages) == 1
    message = notifications.messages[0]
    assert message.title_key == "notification.test_title"
    assert message.body_key == "notification.test_body"
    serialized = json.dumps(response) + repr(message)
    assert "Primary" not in serialized
    assert ADMIN_KEY not in serialized


def test_notification_test_fails_closed_when_adapter_is_unavailable(tmp_path):
    runtime, _database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        status, _, body = request(
            server,
            "/api/v1/notifications/test",
            method="POST",
            document={"profile_id": profile_id},
        )

    assert status == 503
    response = json.loads(body)
    assert response["error"]["code"] == "capability_unavailable"
    assert response["error"]["params"] == {"capability": "notifications"}
    assert ADMIN_KEY.encode() not in body


def test_api_rejects_duplicate_query_unknown_body_and_raw_export_policy(tmp_path):
    runtime, _database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        status, _, body = request(
            server,
            f"/api/v1/history?start_utc={DAY}&start_utc={DAY}&end_utc={DAY + 86_400}",
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "duplicate_query_field"

        status, _, body = request(
            server,
            "/api/v1/sync",
            method="POST",
            document={"profile_id": profile_id, "unexpected": ADMIN_KEY},
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "unknown_request_field"
        assert ADMIN_KEY.encode() not in body

        status, _, body = request(
            server,
            "/api/v1/export",
            method="POST",
            document={
                "format": "json",
                "profile_id": profile_id,
                "start_utc": DAY,
                "end_utc": DAY + 86_400,
                "project_id_policy": "include",
            },
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "unsafe_export_policy"


def test_slow_sync_result_is_rejected_after_active_profile_changes(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    bucket = {
        "start_time": DAY,
        "end_time": DAY + 86_400,
        "results": [
            {
                "project_id": RAW_PROJECT_ID,
                "model": "gpt-4o-mini",
                "service_tier": "priority",
                "input_tokens": 2,
                "output_tokens": 1,
                "num_model_requests": 1,
            }
        ],
    }
    calls = 0

    def factory(key, _timeout):
        nonlocal calls
        calls += 1
        # Credential verification is immediate; the sync client blocks.
        return FakeClient(
            key,
            bucket=bucket if calls >= 2 else None,
            entered=entered if calls >= 2 else None,
            release=release if calls >= 2 else None,
        )

    class CapabilityOnlyVerifier:
        def verify(self, _client):
            return CredentialVerification(True)

    runtime, _database = make_runtime(
        tmp_path,
        client_factory=factory,
        credential_verifier=CapabilityOnlyVerifier(),
    )
    first = runtime.create_profile(display_name="First", admin_key=ADMIN_KEY)["profile_id"]
    runtime.activate_profile(first)
    outcome = []

    def run_sync():
        try:
            outcome.append(runtime.sync_usage(start_utc=DAY, end_utc=DAY + 86_400))
        except Exception as error:
            outcome.append(error)

    worker = threading.Thread(target=run_sync)
    worker.start()
    assert entered.wait(5)
    second_key = "sk-" + "admin-second-secret-12345678"
    second = runtime.create_profile(display_name="Second", admin_key=second_key)["profile_id"]
    runtime.activate_profile(second)
    release.set()
    worker.join(timeout=10)

    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeRequestError)
    assert outcome[0].code == "active_profile_changed"
    assert RAW_PROJECT_ID not in repr(outcome[0])
    assert ADMIN_KEY not in repr(outcome[0])


def test_retention_api_requires_preview_exact_confirmation_and_consumes_token(tmp_path):
    runtime, database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        seed_usage(database, profile_id, runtime.project_keys)

        status, _, body = request(
            server,
            "/api/v1/operations/retention/preview",
            method="POST",
            document={"profile_id": profile_id, "retention_days": 365},
        )
        preview = json.loads(body)
        assert status == 200
        assert preview["profile_id"] == profile_id
        assert preview["row_count"] == 1
        assert preview["cutoff_utc"] > DAY
        assert "database" not in body.decode("utf-8")

        status, _, body = request(
            server,
            "/api/v1/operations/retention/apply",
            method="POST",
            document={"preview_token": preview["preview_token"], "confirm": False},
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "retention_confirmation_required"

        status, _, body = request(
            server,
            "/api/v1/operations/retention/apply",
            method="POST",
            document={"preview_token": preview["preview_token"], "confirm": True},
        )
        result = json.loads(body)
        assert status == 200
        assert result["deleted_rows"] == 1

        status, _, body = request(
            server,
            "/api/v1/operations/retention/apply",
            method="POST",
            document={"preview_token": preview["preview_token"], "confirm": True},
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "retention_preview_expired"


def test_retention_api_rejects_preview_when_history_changes(tmp_path):
    runtime, database = make_runtime(tmp_path)
    with running(runtime) as server:
        profile_id = create_and_activate(server)
        seed_usage(database, profile_id, runtime.project_keys)
        status, _, body = request(
            server,
            "/api/v1/operations/retention/preview",
            method="POST",
            document={"retention_days": 365},
        )
        assert status == 200
        token = json.loads(body)["preview_token"]

        seed_usage_day(database, profile_id, runtime.project_keys, DAY - 86_400)
        status, _, body = request(
            server,
            "/api/v1/operations/retention/apply",
            method="POST",
            document={"preview_token": token, "confirm": True},
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "retention_preview_stale"
        assert len(database.query_usage(DAY - 86_400, DAY + 86_400, profile_id=profile_id)) == 2
