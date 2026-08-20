"""Repeatable performance and accelerated reliability evidence.

The simulated soak exercises time-dependent behavior without claiming to be the
real three-platform 72-hour release gate.  That gate must still attach native
runner evidence.
"""

from __future__ import annotations

import hashlib
import platform
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import DatabaseService, UsageBucket
from .export_service import export_json
from .scheduler import BackoffPolicy, CollectionScheduler, RunResult, RunStatus


DAY_SECONDS = 86_400
DEFAULT_START_UTC = 1_735_689_600  # 2025-01-01T00:00:00Z


@dataclass(frozen=True)
class PerformanceThresholds:
    populate_seconds: float = 60.0
    startup_seconds: float = 2.0
    query_30_days_seconds: float = 2.0
    query_365_days_one_project_seconds: float = 3.0
    export_seconds: float = 15.0
    backup_seconds: float = 15.0


class SimulatedClock:
    def __init__(self, start: datetime):
        self._monotonic = 0.0
        self._utc = start

    def monotonic(self) -> float:
        return self._monotonic

    def utc_now(self) -> datetime:
        return self._utc

    def advance(self, seconds: float, *, wall_clock_adjustment: float = 0.0) -> None:
        self._monotonic += seconds
        self._utc += timedelta(seconds=seconds + wall_clock_adjustment)


def _project_key(index: int) -> str:
    digest = hashlib.sha256(f"synthetic-project-{index}".encode("ascii")).hexdigest()
    return f"project-{digest[:24]}"


def _elapsed(operation):
    started = time.perf_counter()
    result = operation()
    return time.perf_counter() - started, result


def run_performance_harness(
    workspace: Path,
    *,
    days: int = 365,
    projects: int = 100,
    thresholds: PerformanceThresholds | None = None,
) -> dict:
    if not 30 <= days <= 366:
        raise ValueError("days must be between 30 and 366")
    if not 1 <= projects <= 1_000:
        raise ValueError("projects must be between 1 and 1000")
    limits = thresholds or PerformanceThresholds()
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    database_path = workspace / "performance.sqlite3"
    database = DatabaseService(database_path)
    end_utc = DEFAULT_START_UTC + days * DAY_SECONDS

    buckets = [
        UsageBucket(
            bucket_start_utc=DEFAULT_START_UTC + day * DAY_SECONDS,
            bucket_end_utc=DEFAULT_START_UTC + (day + 1) * DAY_SECONDS,
            project_id=f"anon_project_{project:04d}",
            project_name=f"Synthetic Project {project:04d}",
            project_key=_project_key(project),
            model="gpt-4.1-mini",
            service_tier="default",
            input_tokens=project + 1,
            cached_input_tokens=0,
            output_tokens=1,
            request_count=1,
            catalog_version="quality-harness-v1",
            collected_at="2025-01-01T00:00:00Z",
        )
        for day in range(days)
        for project in range(projects)
    ]

    def populate() -> None:
        run_id = database.start_collection_run(
            DEFAULT_START_UTC, end_utc, run_id="quality-performance"
        )
        database.reconcile_slice(
            run_id,
            DEFAULT_START_UTC,
            end_utc,
            buckets,
            pages_fetched=days,
        )
        database.finish_collection_run(run_id, "completed")

    populate_seconds, _ = _elapsed(populate)
    query_start = end_utc - min(30, days) * DAY_SECONDS
    query_30_seconds, recent = _elapsed(
        lambda: database.query_usage(query_start, end_utc)
    )
    one_project_seconds, one_project = _elapsed(
        lambda: database.query_usage(
            DEFAULT_START_UTC,
            end_utc,
            project_key=_project_key(0),
        )
    )
    export_seconds, export_path = _elapsed(
        lambda: export_json(
            database,
            workspace / "performance-export.json",
            DEFAULT_START_UTC,
            end_utc,
        )
    )
    backup_seconds, backup_path = _elapsed(
        lambda: database.backup_to(workspace / "performance.backup.sqlite3")
    )
    startup_seconds, reopened = _elapsed(lambda: DatabaseService(database_path))
    integrity = reopened.check_integrity(full=True)

    timings = {
        "populate_seconds": populate_seconds,
        "startup_seconds": startup_seconds,
        "query_30_days_seconds": query_30_seconds,
        "query_365_days_one_project_seconds": one_project_seconds,
        "export_seconds": export_seconds,
        "backup_seconds": backup_seconds,
    }
    threshold_values = asdict(limits)
    checks = {name: value <= threshold_values[name] for name, value in timings.items()}
    checks.update(
        {
            "record_count": len(recent) == min(30, days) * projects,
            "one_project_count": len(one_project) == days,
            "integrity": integrity.ok,
            "export_nonempty": export_path.stat().st_size > 0,
            "backup_nonempty": backup_path.stat().st_size > 0,
        }
    )
    return {
        "schema_version": 1,
        "kind": "synthetic_performance",
        "passed": all(checks.values()),
        "scenario": {"days": days, "projects": projects, "records": days * projects},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
        },
        "timings": timings,
        "thresholds": threshold_values,
        "checks": checks,
    }


def run_simulated_soak(*, hours: int = 72, interval_seconds: int = 900) -> dict:
    if not 1 <= hours <= 24 * 31:
        raise ValueError("hours must be between 1 and 744")
    clock = SimulatedClock(datetime(2025, 1, 1, tzinfo=timezone.utc))
    calls: list[tuple[float, datetime, RunStatus]] = []

    def collector() -> RunResult:
        index = len(calls)
        if index % 47 == 10:
            result = RunResult(RunStatus.RETRYABLE_ERROR, "rate_limited")
        elif index % 47 == 11:
            result = RunResult(RunStatus.RETRYABLE_ERROR, "upstream_5xx")
        elif index % 31 == 9:
            result = RunResult(RunStatus.PARTIAL, "partial_collection")
        else:
            result = RunResult(RunStatus.SUCCESS)
        calls.append((clock.monotonic(), clock.utc_now(), result.status))
        return result

    scheduler = CollectionScheduler(
        collector,
        interval_seconds=interval_seconds,
        clock=clock,
        backoff=BackoffPolicy(
            base_seconds=30,
            maximum_seconds=300,
            jitter_ratio=0,
        ),
    )
    scheduler.start(run_immediately=True)
    total_seconds = hours * 3_600
    slept = False
    wall_clock_regressed = False
    sleep_resume_runs = 0
    step = 60
    while clock.monotonic() < total_seconds:
        scheduler.tick()
        if not slept and clock.monotonic() >= total_seconds / 3:
            before = len(calls)
            clock.advance(6 * 3_600)
            scheduler.tick()
            sleep_resume_runs = len(calls) - before
            slept = True
            continue
        if not wall_clock_regressed and clock.monotonic() >= total_seconds / 2:
            clock.advance(step, wall_clock_adjustment=-3_600)
            wall_clock_regressed = True
        else:
            clock.advance(step)

    snapshot = scheduler.snapshot()
    unique_monotonic_times = len({stamp for stamp, _, _ in calls}) == len(calls)
    checks = {
        "ran_collections": len(calls) > hours,
        "sleep_resume_single_catchup": sleep_resume_runs == 1,
        "wall_clock_regression_survived": wall_clock_regressed,
        "no_duplicate_monotonic_runs": unique_monotonic_times,
        "scheduler_not_stuck_running": not snapshot.running,
        "bounded_backoff": snapshot.consecutive_failures <= 2,
    }
    return {
        "schema_version": 1,
        "kind": "accelerated_simulated_soak",
        "release_gate_equivalent": False,
        "passed": all(checks.values()),
        "scenario": {
            "simulated_hours": hours,
            "interval_seconds": interval_seconds,
            "sleep_seconds": 6 * 3_600,
            "wall_clock_regression_seconds": 3_600,
        },
        "runs": len(calls),
        "statuses": {
            status.value: sum(1 for _, _, item in calls if item == status)
            for status in RunStatus
        },
        "checks": checks,
    }
