from datetime import datetime, timezone

import pytest

from quota_monitor.scheduler import (
    BackoffPolicy,
    CollectionScheduler,
    RunResult,
    RunStatus,
    SchedulerState,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.wall = datetime(2026, 8, 9, 1, 2, tzinfo=timezone.utc)

    def monotonic(self):
        return self.value

    def utc_now(self):
        return self.wall

    def advance(self, seconds):
        self.value += seconds


def test_interval_has_safe_minimum_and_paused_scheduler_never_runs():
    with pytest.raises(ValueError, match="at least 300"):
        CollectionScheduler(lambda: RunResult(RunStatus.SUCCESS), interval_seconds=60)

    calls = []
    scheduler = CollectionScheduler(lambda: calls.append(1) or RunResult(RunStatus.SUCCESS))
    assert scheduler.tick() is False
    assert scheduler.run_now() is False
    assert calls == []


def test_long_sleep_runs_once_then_schedules_from_current_monotonic_time():
    clock = FakeClock()
    calls = []
    scheduler = CollectionScheduler(
        lambda: calls.append(clock.value) or RunResult(RunStatus.SUCCESS),
        interval_seconds=300,
        clock=clock,
    )
    scheduler.start()
    clock.advance(3600)

    assert scheduler.tick() is True
    assert scheduler.tick() is False
    assert calls == [3700.0]
    assert scheduler.snapshot().next_run_monotonic == 4000.0


def test_retryable_failure_uses_bounded_backoff_and_auth_failure_stops_retry():
    clock = FakeClock()
    results = iter(
        [
            RunResult(RunStatus.RETRYABLE_ERROR, "rate_limited"),
            RunResult(RunStatus.AUTH_ERROR, "credential_revoked"),
        ]
    )
    scheduler = CollectionScheduler(
        lambda: next(results),
        interval_seconds=300,
        clock=clock,
        backoff=BackoffPolicy(base_seconds=10, maximum_seconds=60, jitter_ratio=0, random_source=lambda: 0),
    )
    scheduler.start(run_immediately=True)

    assert scheduler.tick() is True
    assert scheduler.snapshot().next_run_monotonic == 110.0
    clock.advance(10)
    assert scheduler.tick() is True
    snapshot = scheduler.snapshot()
    assert snapshot.state == SchedulerState.ERROR
    assert snapshot.next_run_monotonic is None
    assert scheduler.tick() is False


def test_partial_result_is_stale_and_does_not_update_last_success():
    clock = FakeClock()
    scheduler = CollectionScheduler(
        lambda: RunResult(RunStatus.PARTIAL, "costs_unavailable"),
        interval_seconds=300,
        clock=clock,
    )
    scheduler.start(run_immediately=True)

    assert scheduler.tick() is True
    snapshot = scheduler.snapshot()
    assert snapshot.state == SchedulerState.STALE
    assert snapshot.last_success_at is None
    assert snapshot.last_error_code == "costs_unavailable"
