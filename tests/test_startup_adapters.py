import plistlib
from pathlib import Path

import pytest

from quota_monitor.platform_adapters import (
    CommandResult,
    LinuxStartupAdapter,
    MacOSStartupAdapter,
    WindowsStartupAdapter,
)


class FakeStartupRunner:
    def __init__(self, *, returncode=0):
        self.returncode = returncode
        self.commands = []
        self.writes = {}
        self.removals = []

    def run(self, argv, *, input_text=None):
        self.commands.append((tuple(argv), input_text))
        return CommandResult(self.returncode)

    def write_text(self, operation):
        self.writes[operation.path] = (operation.content, operation.mode)

    def remove(self, path):
        self.removals.append(path)
        return self.writes.pop(path, None) is not None

    def exists(self, path):
        return path in self.writes


def test_windows_startup_is_inert_until_fake_runner_is_invoked(tmp_path):
    runner = FakeStartupRunner()
    executable = (tmp_path / "tracker.exe").resolve()
    adapter = WindowsStartupAdapter(executable, runner=runner)

    plan = adapter.plan_enable()

    assert runner.commands == []
    rendered_command = plan.commands[0]
    assert rendered_command[:2] == ("reg.exe", "ADD")
    assert str(executable) in " ".join(rendered_command)
    assert adapter.enable() is True
    assert runner.commands[0][0] == rendered_command
    assert all(input_text is None for _argv, input_text in runner.commands)


def test_macos_startup_renders_valid_launch_agent_and_uses_fake_runner(tmp_path):
    runner = FakeStartupRunner()
    executable = (tmp_path / "tracker").resolve()
    launch_agents = (tmp_path / "Library" / "LaunchAgents").resolve()
    adapter = MacOSStartupAdapter(
        executable,
        launch_agents,
        uid=501,
        runner=runner,
    )

    payload = plistlib.loads(adapter.render().encode("utf-8"))

    assert payload["ProgramArguments"] == [str(executable), "--background"]
    assert adapter.enable() is True
    assert adapter.target in runner.writes
    assert runner.writes[adapter.target][1] == 0o600
    assert runner.commands[-1][0][:3] == ("launchctl", "bootstrap", "gui/501")
    assert adapter.disable() is True
    assert adapter.target in runner.removals


def test_linux_startup_renders_safe_desktop_entry_and_no_shell(tmp_path):
    runner = FakeStartupRunner()
    executable = (tmp_path / "tracker binary").resolve()
    autostart = (tmp_path / ".config" / "autostart").resolve()
    adapter = LinuxStartupAdapter(executable, autostart, runner=runner)

    rendered = adapter.render()

    assert "Type=Application" in rendered
    escaped_executable = str(executable).replace("\\", "\\\\")
    assert f'Exec="{escaped_executable}" "--background"' in rendered
    assert adapter.enable() is True
    assert runner.commands == []
    assert adapter.is_enabled() is True
    assert adapter.disable() is True
    assert adapter.is_enabled() is False


def test_startup_without_runner_is_explicitly_unavailable(tmp_path):
    adapter = LinuxStartupAdapter(
        (tmp_path / "tracker").resolve(),
        (tmp_path / "autostart").resolve(),
    )

    assert adapter.available is False
    assert adapter.enable() is False
    assert adapter.disable() is False
    assert adapter.is_enabled() is False


def test_startup_arguments_reject_credentials(tmp_path):
    with pytest.raises(ValueError, match="unsafe"):
        LinuxStartupAdapter(
            (tmp_path / "tracker").resolve(),
            (tmp_path / "autostart").resolve(),
            arguments=("--key", "sk-admin-" + "x" * 12),
        )
