from datetime import datetime, timedelta, timezone

import pytest

from quota_monitor.platform_adapters import CredentialReference, InMemoryCredentialStore
from quota_monitor.profiles import (
    CredentialReferenceConflictError,
    InMemoryProfileRepository,
    InMemoryProfileScopedRepository,
    ProfileId,
    ProfileIsolationError,
    ProfileService,
    ScopedRecordNotFoundError,
    new_profile_id,
)


NOW = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)


def _credential(store, profile_id, secret):
    return store.put(str(profile_id), secret)


def test_profile_lifecycle_uses_opaque_ids_and_credential_references_only():
    credentials = InMemoryCredentialStore()
    repository = InMemoryProfileRepository()
    service = ProfileService(repository, clock=lambda: NOW)
    profile_id = new_profile_id()
    reference = _credential(credentials, profile_id, "test-secret-a")

    profile = service.create("Main organization", reference, organization_ref="org_public_ref")
    renamed = service.rename(profile.profile_id, "Production")
    disabled = service.set_enabled(profile.profile_id, False)

    assert profile.profile_id == profile_id
    assert "Main" not in str(profile.profile_id)
    assert profile.credential_ref == reference
    assert renamed.display_name == "Production"
    assert disabled.enabled is False
    assert service.list_enabled() == ()
    assert service.credential_for(profile_id) == reference
    assert "test-secret-a" not in repr(profile)


def test_profile_credential_reference_must_be_owned_by_profile():
    credentials = InMemoryCredentialStore()
    service = ProfileService(InMemoryProfileRepository(), clock=lambda: NOW)
    first_id = new_profile_id()
    second_id = new_profile_id()
    first = service.create("First", _credential(credentials, first_id, "secret-a"))
    second_reference = _credential(credentials, second_id, "secret-b")

    with pytest.raises(ProfileIsolationError):
        service.replace_credential(first.profile_id, second_reference)

    malformed = CredentialReference("cred_opaque", "account-not-a-profile")
    with pytest.raises(ProfileIsolationError):
        service.create("Invalid", malformed)


def test_profile_repository_forbids_credential_sharing():
    credentials = InMemoryCredentialStore()
    repository = InMemoryProfileRepository()
    service = ProfileService(repository, clock=lambda: NOW)
    profile_id = new_profile_id()
    profile = service.create("First", _credential(credentials, profile_id, "secret-a"))

    duplicate_id = new_profile_id()
    duplicate_reference = CredentialReference(
        profile.credential_ref.credential_id,
        str(duplicate_id),
    )
    with pytest.raises(CredentialReferenceConflictError):
        repository.add(
            type(profile)(
                profile_id=duplicate_id,
                display_name="Second",
                credential_ref=duplicate_reference,
                organization_ref=None,
                enabled=True,
                created_at=NOW,
            )
        )


def test_profile_validation_normalizes_utc_and_rejects_key_shaped_metadata():
    credentials = InMemoryCredentialStore()
    local_time = NOW.astimezone(timezone(timedelta(hours=8)))
    service = ProfileService(InMemoryProfileRepository(), clock=lambda: local_time)
    profile_id = new_profile_id()

    profile = service.create("  Taiwan  ", _credential(credentials, profile_id, "secret-a"))

    assert profile.display_name == "Taiwan"
    assert profile.created_at == NOW
    with pytest.raises(ValueError):
        service.create(
            "Unsafe",
            _credential(credentials, new_profile_id(), "secret-b"),
            organization_ref="sk-admin-" + "x" * 12,
        )


def test_profile_scoped_records_with_same_id_are_strictly_isolated():
    repository = InMemoryProfileScopedRepository[dict]()
    profile_a = new_profile_id()
    profile_b = new_profile_id()

    repository.put(profile_a, "project-shared", {"tokens": 10})
    repository.put(profile_b, "project-shared", {"tokens": 99})

    assert repository.get(profile_a, "project-shared") == {"tokens": 10}
    assert repository.get(profile_b, "project-shared") == {"tokens": 99}
    assert repository.list_for_profile(profile_a) == (("project-shared", {"tokens": 10}),)
    repository.delete_profile(profile_a)
    with pytest.raises(ScopedRecordNotFoundError):
        repository.get(profile_a, "project-shared")
    assert repository.get(profile_b, "project-shared") == {"tokens": 99}


def test_profile_scoped_repository_returns_copies():
    repository = InMemoryProfileScopedRepository[dict]()
    profile_id = ProfileId("prof_" + "a" * 32)
    original = {"nested": {"tokens": 1}}
    repository.put(profile_id, "bucket", original)

    original["nested"]["tokens"] = 2
    fetched = repository.get(profile_id, "bucket")
    fetched["nested"]["tokens"] = 3

    assert repository.get(profile_id, "bucket") == {"nested": {"tokens": 1}}
