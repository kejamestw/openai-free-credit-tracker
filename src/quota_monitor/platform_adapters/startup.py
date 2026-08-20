"""Cross-platform startup registration plans with injectable execution."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from .command_runner import CommandResult, CommandRunner, SubprocessCommandRunner


DEFAULT_STARTUP_ID = "com.openai.free-credit-tracker"
_STARTUP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,99}\Z", re.ASCII)


@dataclass(frozen=True)
class FileWrite:
    path: Path
    content: str
    mode: int = 0o600


@dataclass(frozen=True)
class StartupPlan:
    enable: bool
    commands: tuple[tuple[str, ...], ...] = ()
    writes: tuple[FileWrite, ...] = ()
    removals: tuple[Path, ...] = ()


@runtime_checkable
class StartupRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult: ...

    def write_text(self, operation: FileWrite) -> None: ...

    def remove(self, path: Path) -> bool: ...

    def exists(self, path: Path) -> bool: ...


class LocalStartupRunner:
    """Native executor; construction is inert and all mutations require a method call."""

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._commands = command_runner or SubprocessCommandRunner()

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        return self._commands.run(argv, input_text=input_text)

    def write_text(self, operation: FileWrite) -> None:
        operation.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{operation.path.name}.",
            dir=operation.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(operation.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, operation.mode)
            os.replace(temporary_path, operation.path)
        except BaseException:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def remove(self, path: Path) -> bool:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return False
        return True

    def exists(self, path: Path) -> bool:
        return Path(path).is_file()


def _validate_startup_id(value: str) -> str:
    if not isinstance(value, str) or not _STARTUP_ID_PATTERN.fullmatch(value):
        raise ValueError("startup_id is invalid")
    return value


def _validate_program(executable: Path, arguments: Sequence[str]) -> tuple[Path, tuple[str, ...]]:
    program = Path(executable)
    secret_markers = ("sk-" + "admin-", "sk-" + "proj-")
    program_text = str(program)
    if (
        not program.is_absolute()
        or any(character in program_text for character in ("\x00", "\r", "\n"))
        or any(marker in program_text for marker in secret_markers)
    ):
        raise ValueError("startup executable path must be absolute")
    if isinstance(arguments, (str, bytes)):
        raise ValueError("startup arguments must be a sequence of arguments")
    validated_arguments: list[str] = []
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or not argument
            or any(character in argument for character in ("\x00", "\r", "\n"))
            or any(marker in argument for marker in secret_markers)
        ):
            raise ValueError("startup arguments contain an unsafe value")
        validated_arguments.append(argument)
    return program, tuple(validated_arguments)


def _windows_command_line(executable: Path, arguments: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(executable), *arguments])


def _desktop_exec(executable: Path, arguments: Sequence[str]) -> str:
    def quote(value: str) -> str:
        if "%" in value:
            raise ValueError("desktop startup values cannot contain field codes")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return " ".join((quote(str(executable)), *(quote(argument) for argument in arguments)))


class _PlannedStartupAdapter:
    def __init__(self, runner: StartupRunner | None) -> None:
        self._runner = runner

    @property
    def available(self) -> bool:
        return self._runner is not None

    def _apply_enable(self, plan: StartupPlan) -> bool:
        if self._runner is None:
            return False
        written: list[Path] = []
        try:
            for operation in plan.writes:
                self._runner.write_text(operation)
                written.append(operation.path)
            for command in plan.commands:
                if self._runner.run(command).returncode != 0:
                    raise RuntimeError("startup registration command failed")
        except (OSError, RuntimeError):
            for path in reversed(written):
                try:
                    self._runner.remove(path)
                except OSError:
                    pass
            return False
        return True

    def _apply_disable(self, plan: StartupPlan) -> bool:
        if self._runner is None:
            return False
        succeeded = True
        for command in plan.commands:
            if self._runner.run(command).returncode != 0:
                succeeded = False
        for path in plan.removals:
            try:
                self._runner.remove(path)
            except OSError:
                succeeded = False
        return succeeded


class WindowsStartupAdapter(_PlannedStartupAdapter):
    _registry_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(
        self,
        executable: Path,
        arguments: Sequence[str] = ("--background",),
        *,
        runner: StartupRunner | None = None,
        value_name: str = "OpenAIFreeCreditTracker",
    ) -> None:
        super().__init__(runner)
        self._executable, self._arguments = _validate_program(executable, arguments)
        self._value_name = _validate_startup_id(value_name)

    def plan_enable(self) -> StartupPlan:
        command_line = _windows_command_line(self._executable, self._arguments)
        return StartupPlan(
            enable=True,
            commands=(
                (
                    "reg.exe",
                    "ADD",
                    self._registry_key,
                    "/v",
                    self._value_name,
                    "/t",
                    "REG_SZ",
                    "/d",
                    command_line,
                    "/f",
                ),
            ),
        )

    def plan_disable(self) -> StartupPlan:
        return StartupPlan(
            enable=False,
            commands=(
                (
                    "reg.exe",
                    "DELETE",
                    self._registry_key,
                    "/v",
                    self._value_name,
                    "/f",
                ),
            ),
        )

    def enable(self) -> bool:
        return self._apply_enable(self.plan_enable())

    def disable(self) -> bool:
        return self._apply_disable(self.plan_disable())

    def is_enabled(self) -> bool:
        if self._runner is None:
            return False
        result = self._runner.run(
            ("reg.exe", "QUERY", self._registry_key, "/v", self._value_name)
        )
        return result.returncode == 0


class MacOSStartupAdapter(_PlannedStartupAdapter):
    def __init__(
        self,
        executable: Path,
        launch_agents_dir: Path,
        arguments: Sequence[str] = ("--background",),
        *,
        uid: int,
        runner: StartupRunner | None = None,
        startup_id: str = DEFAULT_STARTUP_ID,
    ) -> None:
        super().__init__(runner)
        self._executable, self._arguments = _validate_program(executable, arguments)
        self._startup_id = _validate_startup_id(startup_id)
        self._target = Path(launch_agents_dir) / f"{self._startup_id}.plist"
        if not Path(launch_agents_dir).is_absolute() or uid < 0:
            raise ValueError("launch agent directory and uid are invalid")
        self._uid = uid

    @property
    def target(self) -> Path:
        return self._target

    def render(self) -> str:
        payload = {
            "Label": self._startup_id,
            "ProgramArguments": [str(self._executable), *self._arguments],
            "RunAtLoad": True,
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")

    def plan_enable(self) -> StartupPlan:
        return StartupPlan(
            enable=True,
            writes=(FileWrite(self._target, self.render()),),
            commands=(("launchctl", "bootstrap", f"gui/{self._uid}", str(self._target)),),
        )

    def plan_disable(self) -> StartupPlan:
        return StartupPlan(
            enable=False,
            commands=(("launchctl", "bootout", f"gui/{self._uid}", str(self._target)),),
            removals=(self._target,),
        )

    def enable(self) -> bool:
        return self._apply_enable(self.plan_enable())

    def disable(self) -> bool:
        return self._apply_disable(self.plan_disable())

    def is_enabled(self) -> bool:
        return self._runner is not None and self._runner.exists(self._target)


class LinuxStartupAdapter(_PlannedStartupAdapter):
    def __init__(
        self,
        executable: Path,
        autostart_dir: Path,
        arguments: Sequence[str] = ("--background",),
        *,
        runner: StartupRunner | None = None,
        startup_id: str = DEFAULT_STARTUP_ID,
    ) -> None:
        super().__init__(runner)
        self._executable, self._arguments = _validate_program(executable, arguments)
        self._startup_id = _validate_startup_id(startup_id)
        if not Path(autostart_dir).is_absolute():
            raise ValueError("autostart directory must be absolute")
        self._target = Path(autostart_dir) / f"{self._startup_id}.desktop"

    @property
    def target(self) -> Path:
        return self._target

    def render(self) -> str:
        return "\n".join(
            (
                "[Desktop Entry]",
                "Type=Application",
                "Version=1.0",
                "Name=OpenAI Free Credit Tracker",
                f"Exec={_desktop_exec(self._executable, self._arguments)}",
                "Terminal=false",
                "X-GNOME-Autostart-enabled=true",
                "",
            )
        )

    def plan_enable(self) -> StartupPlan:
        return StartupPlan(enable=True, writes=(FileWrite(self._target, self.render()),))

    def plan_disable(self) -> StartupPlan:
        return StartupPlan(enable=False, removals=(self._target,))

    def enable(self) -> bool:
        return self._apply_enable(self.plan_enable())

    def disable(self) -> bool:
        return self._apply_disable(self.plan_disable())

    def is_enabled(self) -> bool:
        return self._runner is not None and self._runner.exists(self._target)
