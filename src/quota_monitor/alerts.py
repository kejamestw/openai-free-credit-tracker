from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class AlertRule:
    rule_id: str
    profile_id: str
    group_id: str
    threshold_percent: float
    project_key: str = "all"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.rule_id or not self.profile_id or not self.group_id:
            raise ValueError("alert identifiers are required")
        if not 0 < self.threshold_percent <= 100:
            raise ValueError("threshold_percent must be greater than 0 and at most 100")


@dataclass(frozen=True)
class UsageObservation:
    profile_id: str
    group_id: str
    project_key: str
    utc_day: date
    percent: float
    observed_at: datetime
    fresh: bool = True

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.percent < 0:
            raise ValueError("percent must not be negative")


@dataclass(frozen=True)
class AlertEvent:
    rule_id: str
    profile_id: str
    group_id: str
    project_key: str
    threshold_percent: float
    observed_percent: float
    occurred_at: datetime
    test: bool = False

    def safe_payload(self, *, profile_label: str) -> dict:
        return {
            "kind": "quota_threshold_test" if self.test else "quota_threshold",
            "profile_label": profile_label,
            "group": self.group_id,
            "project": "all" if self.project_key == "all" else "selected project",
            "threshold_percent": self.threshold_percent,
            "observed_percent": self.observed_percent,
            "occurred_at": self.occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


class AlertEvaluator:
    """Deduplicate threshold crossings by UTC day, profile, project and rule."""

    def __init__(self) -> None:
        self._previous: dict[tuple[date, str, str, str], float] = {}
        self._sent: set[tuple[date, str]] = set()

    def evaluate(self, observation: UsageObservation, rules: Iterable[AlertRule]) -> list[AlertEvent]:
        key = (
            observation.utc_day,
            observation.profile_id,
            observation.group_id,
            observation.project_key,
        )
        previous = self._previous.get(key, 0.0)
        self._previous[key] = observation.percent
        if not observation.fresh:
            return []

        events = []
        for rule in sorted(rules, key=lambda item: item.threshold_percent):
            if not rule.enabled or rule.profile_id != observation.profile_id or rule.group_id != observation.group_id:
                continue
            if rule.project_key not in {"all", observation.project_key}:
                continue
            sent_key = (observation.utc_day, rule.rule_id)
            if sent_key in self._sent:
                continue
            if previous < rule.threshold_percent <= observation.percent:
                self._sent.add(sent_key)
                events.append(
                    AlertEvent(
                        rule_id=rule.rule_id,
                        profile_id=observation.profile_id,
                        group_id=observation.group_id,
                        project_key=observation.project_key,
                        threshold_percent=rule.threshold_percent,
                        observed_percent=observation.percent,
                        occurred_at=observation.observed_at,
                    )
                )
        return events

    def test_event(self, rule: AlertRule, *, now: datetime | None = None) -> AlertEvent:
        return AlertEvent(
            rule_id=rule.rule_id,
            profile_id=rule.profile_id,
            group_id=rule.group_id,
            project_key=rule.project_key,
            threshold_percent=rule.threshold_percent,
            observed_percent=rule.threshold_percent,
            occurred_at=now or datetime.now(timezone.utc),
            test=True,
        )
