"""SQLite-backed profile metadata repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from quota_monitor.database import DatabaseService
from quota_monitor.platform_adapters import CredentialReference

from .domain import Profile, ProfileId
from .repository import (
    CredentialReferenceConflictError,
    DuplicateProfileError,
    ProfileNotFoundError,
)


class ProfileHasDataError(ValueError):
    """Raised when metadata deletion would orphan profile-scoped history."""


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SQLiteProfileRepository:
    def __init__(self, database: DatabaseService):
        self.database = database

    @staticmethod
    def _from_row(row) -> Profile:
        profile_id = ProfileId(row["profile_id"])
        return Profile(
            profile_id=profile_id,
            display_name=row["display_name"],
            credential_ref=CredentialReference(row["credential_id"], profile_id.value),
            organization_ref=row["organization_ref_private"],
            enabled=bool(row["enabled"]),
            created_at=_parse_utc(row["created_at"]),
            last_validated_at=_parse_utc(row["last_validated_at"]),
        )

    def add(self, profile: Profile) -> None:
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO profiles(
                        profile_id, display_name, credential_id, organization_ref_private,
                        enabled, created_at, last_validated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile.profile_id.value,
                        profile.display_name,
                        profile.credential_ref.credential_id,
                        profile.organization_ref,
                        int(profile.enabled),
                        _utc_text(profile.created_at),
                        _utc_text(profile.last_validated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            with self.database.connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM profiles WHERE profile_id = ?",
                    (profile.profile_id.value,),
                ).fetchone()
            if exists:
                raise DuplicateProfileError(str(profile.profile_id)) from None
            raise CredentialReferenceConflictError(
                "a credential reference cannot be shared between profiles"
            ) from exc

    def get(self, profile_id: ProfileId) -> Profile:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM profiles WHERE profile_id = ?", (profile_id.value,)
            ).fetchone()
        if row is None:
            raise ProfileNotFoundError(str(profile_id))
        return self._from_row(row)

    def save(self, profile: Profile) -> None:
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE profiles
                    SET display_name = ?, credential_id = ?, organization_ref_private = ?,
                        enabled = ?, created_at = ?, last_validated_at = ?
                    WHERE profile_id = ?
                    """,
                    (
                        profile.display_name,
                        profile.credential_ref.credential_id,
                        profile.organization_ref,
                        int(profile.enabled),
                        _utc_text(profile.created_at),
                        _utc_text(profile.last_validated_at),
                        profile.profile_id.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProfileNotFoundError(str(profile.profile_id))
        except sqlite3.IntegrityError as exc:
            raise CredentialReferenceConflictError(
                "a credential reference cannot be shared between profiles"
            ) from exc

    def delete(self, profile_id: ProfileId) -> Profile:
        profile = self.get(profile_id)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM profiles WHERE profile_id = ?", (profile_id.value,)
                )
        except sqlite3.IntegrityError as exc:
            raise ProfileHasDataError(
                "profile history must be handled separately before metadata deletion"
            ) from exc
        return profile

    def list_all(self) -> tuple[Profile, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM profiles ORDER BY created_at, profile_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)
