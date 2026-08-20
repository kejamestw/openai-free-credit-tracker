"""Non-shell command execution boundary for injectable platform adapters."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)


@runtime_checkable
class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run an argv vector directly; shell expansion and command logging are disabled."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv must contain non-empty strings")
        try:
            completed = subprocess.run(
                list(argv),
                input=input_text,
                text=True,
                capture_output=True,
                shell=False,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return CommandResult(returncode=-1)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
