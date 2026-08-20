"""Persistent, profile-scoped alert rules, deduplication, and safe history."""

from __future__ import annotations

import re
import uuid
from datetime import timezone

from .alerts import AlertEvent, AlertRule, UsageObservation
from .database import DatabaseService, utc_now_text


_SAFE_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DELIVERY_STATUSES = frozenset({"sent", "failed", "suppressed", "test"})


class SQLiteAlertState:
    def __init__(self, database: DatabaseService):
        self.database = database

    def save_rule(self, rule: AlertRule) -> None:
        stamp = utc_now_text()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO alert_rules(
                    profile_id, rule_id, group_id, project_key,
                    threshold_percent, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, rule_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    project_key = excluded.project_key,
                    threshold_percent = excluded.threshold_percent,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    rule.profile_id,
                    rule.rule_id,
                    rule.group_id,
                    rule.project_key,
                    rule.threshold_percent,
                    int(rule.enabled),
                    stamp,
                    stamp,
                ),
            )

    def list_rules(self, profile_id: str) -> tuple[AlertRule, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT rule_id, profile_id, group_id, threshold_percent,
                       project_key, enabled
                FROM alert_rules WHERE profile_id = ?
                ORDER BY threshold_percent, rule_id
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            AlertRule(
                row["rule_id"],
                row["profile_id"],
                row["group_id"],
                row["threshold_percent"],
                row["project_key"],
                bool(row["enabled"]),
            )
            for row in rows
        )

    def delete_rule(self, profile_id: str, rule_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM alert_rules WHERE profile_id = ? AND rule_id = ?",
                (profile_id, rule_id),
            )
        return cursor.rowcount == 1

    def evaluate(
        self, observation: UsageObservation, rules: tuple[AlertRule, ...]
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        utc_day = observation.utc_day.isoformat()
        with self.database.transaction() as connection:
            for rule in sorted(rules, key=lambda item: item.threshold_percent):
                if (
                    not rule.enabled
                    or rule.profile_id != observation.profile_id
                    or rule.group_id != observation.group_id
                    or rule.project_key not in {"all", observation.project_key}
                ):
                    continue
                row = connection.execute(
                    """
                    SELECT previous_percent, sent_at FROM alert_dedup
                    WHERE profile_id = ? AND rule_id = ? AND utc_day = ?
                    """,
                    (observation.profile_id, rule.rule_id, utc_day),
                ).fetchone()
                previous = float(row["previous_percent"]) if row else 0.0
                sent_at = row["sent_at"] if row else None
                connection.execute(
                    """
                    INSERT INTO alert_dedup(
                        profile_id, rule_id, utc_day, group_id, project_key,
                        previous_percent, sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, rule_id, utc_day) DO UPDATE SET
                        group_id = excluded.group_id,
                        project_key = excluded.project_key,
                        previous_percent = excluded.previous_percent
                    """,
                    (
                        observation.profile_id,
                        rule.rule_id,
                        utc_day,
                        observation.group_id,
                        observation.project_key,
                        observation.percent,
                        sent_at,
                    ),
                )
                if (
                    observation.fresh
                    and sent_at is None
                    and previous < rule.threshold_percent <= observation.percent
                ):
                    occurred = observation.observed_at.astimezone(timezone.utc)
                    connection.execute(
                        """
                        UPDATE alert_dedup SET sent_at = ?
                        WHERE profile_id = ? AND rule_id = ? AND utc_day = ?
                        """,
                        (
                            utc_now_text(occurred),
                            observation.profile_id,
                            rule.rule_id,
                            utc_day,
                        ),
                    )
                    events.append(
                        AlertEvent(
                            rule.rule_id,
                            observation.profile_id,
                            observation.group_id,
                            observation.project_key,
                            rule.threshold_percent,
                            observation.percent,
                            occurred,
                        )
                    )
        return events

    def record_notification(
        self,
        event: AlertEvent,
        *,
        delivery_status: str,
        error_code: str | None = None,
        notification_id: str | None = None,
    ) -> str:
        if delivery_status not in _DELIVERY_STATUSES:
            raise ValueError("invalid notification delivery status")
        if error_code is not None and not _SAFE_ERROR_CODE.fullmatch(error_code):
            raise ValueError("notification error_code must be a sanitized code")
        identifier = notification_id or uuid.uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO notification_history(
                    profile_id, notification_id, rule_id, event_kind, group_id,
                    project_key, occurred_at, delivery_status, error_code, is_test
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.profile_id,
                    identifier,
                    event.rule_id,
                    "quota_threshold_test" if event.test else "quota_threshold",
                    event.group_id,
                    event.project_key,
                    utc_now_text(event.occurred_at),
                    delivery_status,
                    error_code,
                    int(event.test),
                ),
            )
        return identifier

    def notification_history(self, profile_id: str, *, limit: int = 100) -> tuple[dict, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("notification history limit must be between 1 and 1000")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT notification_id, rule_id, event_kind, group_id, project_key,
                       occurred_at, delivery_status, error_code, is_test
                FROM notification_history
                WHERE profile_id = ?
                ORDER BY occurred_at DESC, notification_id DESC
                LIMIT ?
                """,
                (profile_id, limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)
