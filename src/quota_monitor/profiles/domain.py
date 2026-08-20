"""Profile domain values with no dependency on persistence or UI."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from quota_monitor.platform_adapters import CredentialReference


PROFILE_ID_PATTERN = re.compile(r"prof_[0-9a-f]{32}\Z", re.ASCII)


def _validate_text(value: str, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError(f"{field_name} contains a control character")
    return cleaned


def _validate_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, order=True)
class ProfileId:
    """An opaque local identifier that reveals no organization metadata."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not PROFILE_ID_PATTERN.fullmatch(self.value):
            raise ValueError("profile_id must be an opaque prof_ identifier")

    def __str__(self) -> str:
        return self.value


def new_profile_id() -> ProfileId:
    return ProfileId(f"prof_{uuid4().hex}")


@dataclass(frozen=True)
class Profile:
    profile_id: ProfileId
    display_name: str
    credential_ref: CredentialReference
    organization_ref: str | None
    enabled: bool
    created_at: datetime
    last_validated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.credential_ref.account_id != self.profile_id.value:
            raise ValueError("credential reference must belong to profile_id")
        object.__setattr__(
            self,
            "display_name",
            _validate_text(self.display_name, "display_name", maximum=80),
        )
        if self.organization_ref is not None:
            organization_ref = _validate_text(
                self.organization_ref,
                "organization_ref",
                maximum=200,
            )
            if organization_ref.startswith(("sk-admin-", "sk-proj-")):
                raise ValueError("organization_ref must not contain an API key")
            object.__setattr__(self, "organization_ref", organization_ref)
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        object.__setattr__(self, "created_at", _validate_utc(self.created_at, "created_at"))
        if self.last_validated_at is not None:
            object.__setattr__(
                self,
                "last_validated_at",
                _validate_utc(self.last_validated_at, "last_validated_at"),
            )
            if self.last_validated_at < self.created_at:
                raise ValueError("last_validated_at cannot be earlier than created_at")

    def renamed(self, display_name: str) -> "Profile":
        return replace(self, display_name=display_name)

    def with_enabled(self, enabled: bool) -> "Profile":
        return replace(self, enabled=enabled)

    def with_credential(self, credential_ref: CredentialReference) -> "Profile":
        return replace(self, credential_ref=credential_ref)

    def with_organization_ref(self, organization_ref: str | None) -> "Profile":
        return replace(self, organization_ref=organization_ref)

    def validated_at(self, value: datetime) -> "Profile":
        return replace(self, last_validated_at=value)
