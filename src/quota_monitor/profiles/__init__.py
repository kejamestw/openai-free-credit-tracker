"""Local multi-profile domain and repository building blocks."""

from .domain import PROFILE_ID_PATTERN, Profile, ProfileId, new_profile_id
from .repository import (
    CredentialReferenceConflictError,
    DuplicateProfileError,
    InMemoryProfileRepository,
    ProfileNotFoundError,
    ProfileRepository,
)
from .scoped_repository import InMemoryProfileScopedRepository, ScopedRecordNotFoundError
from .service import ProfileIsolationError, ProfileService
from .sqlite_repository import ProfileHasDataError, SQLiteProfileRepository

__all__ = [
    "CredentialReferenceConflictError",
    "DuplicateProfileError",
    "InMemoryProfileRepository",
    "InMemoryProfileScopedRepository",
    "PROFILE_ID_PATTERN",
    "Profile",
    "ProfileId",
    "ProfileIsolationError",
    "ProfileHasDataError",
    "ProfileNotFoundError",
    "ProfileRepository",
    "ProfileService",
    "SQLiteProfileRepository",
    "ScopedRecordNotFoundError",
    "new_profile_id",
]
