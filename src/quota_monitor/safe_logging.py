from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ADMIN_KEY_PATTERN = re.compile(r"sk-admin-[A-Za-z0-9_-]+", re.IGNORECASE)
BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]+")
ALLOWED_FIELDS = frozenset(
    {
        "request_id",
        "status",
        "code",
        "method",
        "pages_fetched",
        "duration_ms",
        "profile_ref",
        "run_id",
        "state",
        "version",
    }
)


def redact_text(value: object) -> str:
    text = str(value)
    text = ADMIN_KEY_PATTERN.sub("[REDACTED_ADMIN_KEY]", text)
    return BEARER_PATTERN.sub("Bearer [REDACTED]", text)


def _safe_value(name: str, value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = redact_text(value)
    if name == "profile_ref" and len(text) > 12:
        text = f"{text[:6]}…{text[-4:]}"
    return text[:512]


def safe_event(event: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(event, str) or not event or len(event) > 80:
        raise ValueError("event name is invalid")
    output: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": redact_text(event),
    }
    for name, value in (fields or {}).items():
        if name not in ALLOWED_FIELDS:
            continue
        output[name] = _safe_value(name, value)
    return output


class SafeEventLogger:
    """Append allowlisted JSON events with bounded local retention."""

    def __init__(self, path: Path, *, maximum_bytes: int = 1_000_000, backup_count: int = 3):
        if maximum_bytes < 1024 or not 0 <= backup_count <= 10:
            raise ValueError("invalid log retention")
        self.path = path
        self.maximum_bytes = maximum_bytes
        self.backup_count = backup_count

    def emit(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe_event(event, fields), ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        if self.path.exists() and self.path.stat().st_size + len(encoded) > self.maximum_bytes:
            self._rotate()
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)

    def _rotate(self) -> None:
        if self.backup_count == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
