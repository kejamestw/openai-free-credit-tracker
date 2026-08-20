from datetime import date, datetime, timezone

from quota_monitor.alerts import AlertEvaluator, AlertRule, UsageObservation


NOW = datetime(2026, 8, 9, 3, 4, tzinfo=timezone.utc)
RULES = [
    AlertRule("r50", "profile-a", "mini", 50),
    AlertRule("r80", "profile-a", "mini", 80),
    AlertRule("r100", "profile-a", "mini", 100),
]


def observation(percent, *, day=date(2026, 8, 9), fresh=True):
    return UsageObservation("profile-a", "mini", "all", day, percent, NOW, fresh)


def test_alerts_fire_only_on_crossing_and_once_per_utc_day():
    evaluator = AlertEvaluator()

    assert evaluator.evaluate(observation(49), RULES) == []
    assert [event.rule_id for event in evaluator.evaluate(observation(85), RULES)] == ["r50", "r80"]
    assert evaluator.evaluate(observation(45), RULES) == []
    assert evaluator.evaluate(observation(85), RULES) == []
    assert [event.rule_id for event in evaluator.evaluate(observation(100), RULES)] == ["r100"]


def test_alert_dedup_resets_on_utc_day_and_stale_data_never_notifies():
    evaluator = AlertEvaluator()
    assert evaluator.evaluate(observation(90, fresh=False), RULES) == []
    assert evaluator.evaluate(observation(90), RULES) == []
    next_day = date(2026, 8, 10)
    assert [event.rule_id for event in evaluator.evaluate(observation(80, day=next_day), RULES)] == ["r50", "r80"]


def test_test_notification_is_safe_and_does_not_consume_real_dedup_state():
    evaluator = AlertEvaluator()
    event = evaluator.test_event(RULES[0], now=NOW)

    assert event.test is True
    assert event.safe_payload(profile_label="Work")["project"] == "all"
    assert [item.rule_id for item in evaluator.evaluate(observation(60), RULES)] == ["r50"]
