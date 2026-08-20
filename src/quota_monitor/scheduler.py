from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol


MINIMUM_INTERVAL_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 900


class SchedulerState(str, Enum):
    MONITORING = "monitoring"
    PAUSED = "paused"
    SYNCING = "syncing"
    STALE = "stale"
    ERROR = "error"


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    RETRYABLE_ERROR = "retryable_error"
    AUTH_ERROR = "auth_error"


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    error_code: str | None = None


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def utc_now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SchedulerSnapshot:
    state: SchedulerState
    next_run_monotonic: float | None
    last_success_at: datetime | None
    last_error_code: str | None
    consecutive_failures: int
    running: bool


class BackoffPolicy:
    def __init__(
        self,
        *,
        base_seconds: float = 30,
        maximum_seconds: float = 1800,
        jitter_ratio: float = 0.2,
        random_source: Callable[[], float] = random.random,
    ):
        if base_seconds <= 0 or maximum_seconds < base_seconds:
            raise ValueError("invalid backoff bounds")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        self.base_seconds = base_seconds
        self.maximum_seconds = maximum_seconds
        self.jitter_ratio = jitter_ratio
        self.random_source = random_source

    def delay(self, failures: int) -> float:
        if failures < 1:
            return 0
        raw = min(self.maximum_seconds, self.base_seconds * (2 ** (failures - 1)))
        jitter = raw * self.jitter_ratio * self.random_source()
        return min(self.maximum_seconds, raw + jitter)


class CollectionScheduler:
    """A deterministic, monotonic scheduler that never overlaps collection runs."""

    def __init__(
        self,
        collector: Callable[[], RunResult],
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        clock: Clock | None = None,
        backoff: BackoffPolicy | None = None,
    ):
        if interval_seconds < MINIMUM_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MINIMUM_INTERVAL_SECONDS} seconds")
        self.collector = collector
        self.interval_seconds = interval_seconds
        self.clock = clock or SystemClock()
        self.backoff = backoff or BackoffPolicy()
        self._lock = threading.Lock()
        self._paused = True
        self._running = False
        self._next_run: float | None = None
        self._last_success: datetime | None = None
        self._last_error_code: str | None = None
        self._failures = 0
        self._state = SchedulerState.PAUSED

    def start(self, *, run_immediately: bool = False) -> None:
        with self._lock:
            self._paused = False
            now = self.clock.monotonic()
            self._next_run = now if run_immediately else now + self.interval_seconds
            self._state = SchedulerState.MONITORING

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._next_run = None
            self._state = SchedulerState.PAUSED

    def resume(self, *, run_immediately: bool = True) -> None:
        self.start(run_immediately=run_immediately)

    def tick(self) -> bool:
        """Run at most one collection after an interval or a long sleep/resume gap."""

        with self._lock:
            if self._paused or self._running or self._next_run is None:
                return False
            if self.clock.monotonic() < self._next_run:
                return False
        return self._run()

    def run_now(self) -> bool:
        with self._lock:
            if self._paused or self._running:
                return False
        return self._run()

    def _run(self) -> bool:
        with self._lock:
            if self._paused or self._running:
                return False
            self._running = True
            self._state = SchedulerState.SYNCING
        try:
            result = self.collector()
            if not isinstance(result, RunResult):
                raise TypeError("collector must return RunResult")
        except Exception:
            result = RunResult(RunStatus.RETRYABLE_ERROR, "collection_failed")
        now = self.clock.monotonic()
        with self._lock:
            self._running = False
            if result.status == RunStatus.SUCCESS:
                self._failures = 0
                self._last_error_code = None
                self._last_success = self.clock.utc_now()
                self._next_run = now + self.interval_seconds
                self._state = SchedulerState.MONITORING
            elif result.status == RunStatus.PARTIAL:
                self._failures = 0
                self._last_error_code = result.error_code or "partial_collection"
                self._next_run = now + self.interval_seconds
                self._state = SchedulerState.STALE
            elif result.status == RunStatus.AUTH_ERROR:
                self._paused = True
                self._next_run = None
                self._last_error_code = result.error_code or "credential_invalid"
                self._state = SchedulerState.ERROR
            else:
                self._failures += 1
                self._last_error_code = result.error_code or "collection_failed"
                self._next_run = now + self.backoff.delay(self._failures)
                self._state = SchedulerState.ERROR
        return True

    def snapshot(self) -> SchedulerSnapshot:
        with self._lock:
            return SchedulerSnapshot(
                state=self._state,
                next_run_monotonic=self._next_run,
                last_success_at=self._last_success,
                last_error_code=self._last_error_code,
                consecutive_failures=self._failures,
                running=self._running,
            )
