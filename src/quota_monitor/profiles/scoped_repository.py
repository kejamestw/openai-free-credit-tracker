"""A generic profile-scoped store used to enforce query isolation by design."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Generic, TypeVar

from .domain import ProfileId


T = TypeVar("T")


class ScopedRecordNotFoundError(KeyError):
    pass


class InMemoryProfileScopedRepository(Generic[T]):
    """Store records under a mandatory ``(profile_id, record_id)`` key.

    There is intentionally no unscoped list/get API.  Identical project or
    bucket IDs in two organizations therefore cannot collide or leak.
    """

    def __init__(self) -> None:
        self._records: dict[ProfileId, dict[str, T]] = {}
        self._lock = RLock()

    @staticmethod
    def _validate_record_id(record_id: str) -> str:
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("record_id must be non-empty")
        if len(record_id) > 500 or any(ord(character) < 32 for character in record_id):
            raise ValueError("record_id is invalid")
        return record_id

    def put(self, profile_id: ProfileId, record_id: str, value: T) -> None:
        key = self._validate_record_id(record_id)
        with self._lock:
            self._records.setdefault(profile_id, {})[key] = deepcopy(value)

    def get(self, profile_id: ProfileId, record_id: str) -> T:
        key = self._validate_record_id(record_id)
        with self._lock:
            try:
                return deepcopy(self._records[profile_id][key])
            except KeyError:
                raise ScopedRecordNotFoundError((str(profile_id), key)) from None

    def list_for_profile(self, profile_id: ProfileId) -> tuple[tuple[str, T], ...]:
        with self._lock:
            rows = self._records.get(profile_id, {})
            return tuple((key, deepcopy(rows[key])) for key in sorted(rows))

    def delete(self, profile_id: ProfileId, record_id: str) -> bool:
        key = self._validate_record_id(record_id)
        with self._lock:
            rows = self._records.get(profile_id)
            if rows is None or key not in rows:
                return False
            del rows[key]
            if not rows:
                del self._records[profile_id]
            return True

    def delete_profile(self, profile_id: ProfileId) -> int:
        with self._lock:
            rows = self._records.pop(profile_id, {})
            return len(rows)
