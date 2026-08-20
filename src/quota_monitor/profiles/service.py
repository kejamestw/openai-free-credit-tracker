"""Profile lifecycle operations and credential ownership enforcement."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from quota_monitor.platform_adapters import CredentialReference

from .domain import Profile, ProfileId
from .repository import ProfileRepository


class ProfileIsolationError(ValueError):
    """Raised when a credential reference is not owned by its profile."""


class ProfileService:
    def __init__(
        self,
        repository: ProfileRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _profile_id_for(reference: CredentialReference) -> ProfileId:
        try:
            return ProfileId(reference.account_id)
        except ValueError:
            raise ProfileIsolationError(
                "credential account_id must be the owning opaque profile_id"
            ) from None

    @staticmethod
    def _assert_owner(profile_id: ProfileId, reference: CredentialReference) -> None:
        if reference.account_id != profile_id.value:
            raise ProfileIsolationError("credential reference belongs to another profile")

    def create(
        self,
        display_name: str,
        credential_ref: CredentialReference,
        *,
        organization_ref: str | None = None,
        enabled: bool = True,
    ) -> Profile:
        profile_id = self._profile_id_for(credential_ref)
        profile = Profile(
            profile_id=profile_id,
            display_name=display_name,
            credential_ref=credential_ref,
            organization_ref=organization_ref,
            enabled=enabled,
            created_at=self._clock(),
        )
        self._repository.add(profile)
        return profile

    def get(self, profile_id: ProfileId) -> Profile:
        return self._repository.get(profile_id)

    def list_all(self) -> tuple[Profile, ...]:
        return self._repository.list_all()

    def list_enabled(self) -> tuple[Profile, ...]:
        return tuple(profile for profile in self.list_all() if profile.enabled)

    def rename(self, profile_id: ProfileId, display_name: str) -> Profile:
        profile = self.get(profile_id).renamed(display_name)
        self._repository.save(profile)
        return profile

    def set_enabled(self, profile_id: ProfileId, enabled: bool) -> Profile:
        profile = self.get(profile_id).with_enabled(enabled)
        self._repository.save(profile)
        return profile

    def replace_credential(
        self,
        profile_id: ProfileId,
        credential_ref: CredentialReference,
    ) -> Profile:
        self._assert_owner(profile_id, credential_ref)
        profile = self.get(profile_id).with_credential(credential_ref)
        self._repository.save(profile)
        return profile

    def set_organization_ref(
        self,
        profile_id: ProfileId,
        organization_ref: str | None,
    ) -> Profile:
        profile = self.get(profile_id).with_organization_ref(organization_ref)
        self._repository.save(profile)
        return profile

    def credential_for(self, profile_id: ProfileId) -> CredentialReference:
        profile = self.get(profile_id)
        self._assert_owner(profile_id, profile.credential_ref)
        return profile.credential_ref

    def mark_validated(
        self,
        profile_id: ProfileId,
        at: datetime | None = None,
    ) -> Profile:
        profile = self.get(profile_id).validated_at(at or self._clock())
        self._repository.save(profile)
        return profile

    def delete(self, profile_id: ProfileId) -> Profile:
        """Delete profile metadata; credential cleanup remains an explicit caller step."""

        return self._repository.delete(profile_id)
