"""Profile repository contract and deterministic in-memory implementation."""

from __future__ import annotations

from threading import RLock
from typing import Protocol, runtime_checkable

from .domain import Profile, ProfileId


class ProfileNotFoundError(KeyError):
    pass


class DuplicateProfileError(ValueError):
    pass


class CredentialReferenceConflictError(ValueError):
    pass


@runtime_checkable
class ProfileRepository(Protocol):
    def add(self, profile: Profile) -> None: ...

    def get(self, profile_id: ProfileId) -> Profile: ...

    def save(self, profile: Profile) -> None: ...

    def delete(self, profile_id: ProfileId) -> Profile: ...

    def list_all(self) -> tuple[Profile, ...]: ...


class InMemoryProfileRepository:
    def __init__(self) -> None:
        self._profiles: dict[ProfileId, Profile] = {}
        self._lock = RLock()

    def _credential_owner(self, profile: Profile) -> ProfileId | None:
        for candidate in self._profiles.values():
            if candidate.credential_ref.credential_id == profile.credential_ref.credential_id:
                return candidate.profile_id
        return None

    def add(self, profile: Profile) -> None:
        with self._lock:
            if profile.profile_id in self._profiles:
                raise DuplicateProfileError(str(profile.profile_id))
            owner = self._credential_owner(profile)
            if owner is not None:
                raise CredentialReferenceConflictError(
                    "a credential reference cannot be shared between profiles"
                )
            self._profiles[profile.profile_id] = profile

    def get(self, profile_id: ProfileId) -> Profile:
        with self._lock:
            try:
                return self._profiles[profile_id]
            except KeyError:
                raise ProfileNotFoundError(str(profile_id)) from None

    def save(self, profile: Profile) -> None:
        with self._lock:
            if profile.profile_id not in self._profiles:
                raise ProfileNotFoundError(str(profile.profile_id))
            owner = self._credential_owner(profile)
            if owner is not None and owner != profile.profile_id:
                raise CredentialReferenceConflictError(
                    "a credential reference cannot be shared between profiles"
                )
            self._profiles[profile.profile_id] = profile

    def delete(self, profile_id: ProfileId) -> Profile:
        with self._lock:
            try:
                return self._profiles.pop(profile_id)
            except KeyError:
                raise ProfileNotFoundError(str(profile_id)) from None

    def list_all(self) -> tuple[Profile, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._profiles.values(),
                    key=lambda profile: (profile.created_at, profile.profile_id.value),
                )
            )
