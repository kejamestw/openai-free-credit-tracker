from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_installer_is_per_user_and_preserves_application_data():
    source = (ROOT / "installer" / "windows" / "OpenAI-Free-Credit-Tracker.iss").read_text(
        encoding="utf-8"
    )

    assert "PrivilegesRequired=lowest" in source
    assert "{localappdata}\\Programs" in source
    assert "AppId={{9C47963B-F3AD-49A1-B2F2-4D427980A018}" in source
    assert "[UninstallDelete]" in source
    assert "{appdata}" not in source.split("[UninstallDelete]", 1)[1]
    assert "{localappdata}" not in source.split("[UninstallDelete]", 1)[1]


def test_installer_build_uses_the_same_one_file_executable_and_smokes_lifecycle():
    source = (ROOT / "scripts" / "build_installer_windows.bat").read_text(encoding="utf-8")

    assert "call scripts\\build_windows.bat" in source
    assert "dist\\OpenAI-Free-Credit-Tracker.exe" in source
    assert "--version" in source
    assert "unins000.exe" in source
    assert "if errorlevel 1 exit /b" in source
