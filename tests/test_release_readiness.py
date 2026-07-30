import os
import sys
import subprocess
import tomllib
from pathlib import Path

from quota_monitor import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_tracked_text_has_no_unexpected_ascii_control_characters():
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = [path for path in result.stdout.split(b"\0") if path]
    findings = []
    for encoded_path in paths:
        path = ROOT / encoded_path.decode("utf-8")
        if not path.is_file():
            continue
        for offset, value in enumerate(path.read_bytes()):
            if (value < 32 and value not in {9, 10, 13}) or value == 127:
                findings.append(f"{path.relative_to(ROOT)}:{offset}:0x{value:02x}")
    assert findings == []


def test_pyproject_uses_package_version_as_its_dynamic_source():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "quota_monitor.version.__version__"
    }
    assert __version__ == "0.1.0"
    assert not (ROOT / "VERSION").exists()


def test_windows_build_is_fail_fast_and_runs_packaged_smoke_test():
    script = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")
    assert script.count("if errorlevel 1 exit /b 1") >= 6
    assert "src\\quota_monitor\\app.py" in script
    assert '"dist\\OpenAI-Free-Credit-Tracker.exe" --smoke-test' in script
    assert '"dist\\OpenAI-Free-Credit-Tracker.exe" --version' in script
    assert "explorer" not in script.lower()
    assert "pause" not in script.lower()


def test_source_smoke_test_validates_all_runtime_resources():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    result = subprocess.run(
        [sys.executable, "-m", "quota_monitor", "--smoke-test"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert f"{__version__} smoke test passed" in result.stdout


def test_repository_audit_passes():
    result = subprocess.run(
        [sys.executable, "scripts/audit_repository.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "repository audit passed" in result.stdout
