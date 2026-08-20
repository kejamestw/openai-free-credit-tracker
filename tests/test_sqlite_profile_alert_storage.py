from datetime import date, datetime, timezone

import pytest

from quota_monitor.alert_storage import SQLiteAlertState
from quota_monitor.alerts import AlertRule, UsageObservation
from quota_monitor.database import DatabaseService, UsageBucket
from quota_monitor.platform_adapters import CredentialReference
from quota_monitor.profiles import (
    CredentialReferenceConflictError,
    ProfileHasDataError,
    ProfileId,
    ProfileService,
    SQLiteProfileRepository,
)
from quota_monitor.upstream_adapter import ProjectKeyDeriver


NOW = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)
START = int(datetime(2026, 8, 9, tzinfo=timezone.utc).timestamp())
DAY = 86_400
PROFILE_A = ProfileId("prof_" + "a" * 32)
PROFILE_B = ProfileId("prof_" + "b" * 32)


def create_profile(service, profile_id, credential_id, name):
    return service.create(
        name,
        CredentialReference(credential_id, profile_id.value),
        organization_ref=f"org-{name.lower()}",
    )


def test_sqlite_profile_repository_round_trip_update_and_credential_isolation(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    repository = SQLiteProfileRepository(database)
    profiles = ProfileService(repository, clock=lambda: NOW)

    first = create_profile(profiles, PROFILE_A, "cred-a", "Alpha")
    create_profile(profiles, PROFILE_B, "cred-b", "Beta")
    renamed = profiles.rename(PROFILE_A, "Primary")
    disabled = profiles.set_enabled(PROFILE_B, False)

    assert repository.get(PROFILE_A).credential_ref == first.credential_ref
    assert renamed.display_name == "Primary"
    assert disabled.enabled is False
    assert {profile.profile_id for profile in repository.list_all()} >= {PROFILE_A, PROFILE_B}

    conflicting = type(first)(
        profile_id=ProfileId("prof_" + "c" * 32),
        display_name="Conflict",
        credential_ref=CredentialReference("cred-a", "prof_" + "c" * 32),
        organization_ref=None,
        enabled=True,
        created_at=NOW,
    )
    with pytest.raises(CredentialReferenceConflictError):
        repository.add(conflicting)

    deleted = repository.delete(PROFILE_B)
    assert deleted.profile_id == PROFILE_B


def test_profile_metadata_delete_is_blocked_while_history_exists(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    repository = SQLiteProfileRepository(database)
    profiles = ProfileService(repository, clock=lambda: NOW)
    create_profile(profiles, PROFILE_A, "cred-a", "Alpha")
    project_id = "proj_private"
    project_key = ProjectKeyDeriver(b"0123456789abcdef").derive(project_id)
    value = UsageBucket(
        START,
        START + 3600,
        project_id,
        "Private",
        "gpt-5.6-terra",
        "incentivized-tier",
        100,
        0,
        5,
        1,
        "catalog-v1",
        "2026-08-09T01:00:00Z",
        project_key,
    )
    database.start_collection_run(
        START, START + DAY, run_id="profile-a-run", profile_id=PROFILE_A.value
    )
    database.reconcile_slice(
        "profile-a-run",
        START,
        START + DAY,
        [value],
        pages_fetched=1,
        profile_id=PROFILE_A.value,
    )

    with pytest.raises(ProfileHasDataError, match="history"):
        repository.delete(PROFILE_A)


def test_sqlite_alert_state_persists_dedup_and_isolates_profiles(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    repository = SQLiteProfileRepository(database)
    profiles = ProfileService(repository, clock=lambda: NOW)
    create_profile(profiles, PROFILE_A, "cred-a", "Alpha")
    create_profile(profiles, PROFILE_B, "cred-b", "Beta")
    state = SQLiteAlertState(database)
    rule_a = AlertRule("rule-50", PROFILE_A.value, "mini", 50)
    rule_b = AlertRule("rule-50", PROFILE_B.value, "mini", 50)
    state.save_rule(rule_a)
    state.save_rule(rule_b)

    first_a = UsageObservation(
        PROFILE_A.value, "mini", "all", date(2026, 8, 9), 60, NOW
    )
    first_b = UsageObservation(
        PROFILE_B.value, "mini", "all", date(2026, 8, 9), 80, NOW
    )
    event_a = state.evaluate(first_a, (rule_a,))[0]
    event_b = state.evaluate(first_b, (rule_b,))[0]
    assert state.evaluate(first_a, (rule_a,)) == []
    assert event_a.profile_id == PROFILE_A.value
    assert event_b.profile_id == PROFILE_B.value

    state.record_notification(event_a, delivery_status="sent", notification_id="notice-a")
    state.record_notification(event_b, delivery_status="sent", notification_id="notice-b")
    assert [row["notification_id"] for row in state.notification_history(PROFILE_A.value)] == [
        "notice-a"
    ]
    assert [row["notification_id"] for row in state.notification_history(PROFILE_B.value)] == [
        "notice-b"
    ]


def test_notification_history_rejects_unsafe_error_fields(tmp_path):
    database = DatabaseService(tmp_path / "history.sqlite3")
    profiles = ProfileService(SQLiteProfileRepository(database), clock=lambda: NOW)
    create_profile(profiles, PROFILE_A, "cred-a", "Alpha")
    state = SQLiteAlertState(database)
    rule = AlertRule("rule-50", PROFILE_A.value, "mini", 50)
    state.save_rule(rule)
    event = state.evaluate(
        (
            UsageObservation(
                PROFILE_A.value, "mini", "all", date(2026, 8, 9), 60, NOW
            )
        ),
        (rule,),
    )[0]

    with pytest.raises(ValueError, match="sanitized"):
        state.record_notification(
            event,
            delivery_status="failed",
            error_code="sk-admin-secret response body",
        )
    assert state.notification_history(PROFILE_A.value) == ()
