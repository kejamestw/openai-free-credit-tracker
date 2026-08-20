from pathlib import Path

import pytest

from quota_monitor.platform_paths import resolve_app_paths


def test_windows_paths_use_roaming_for_config_and_local_for_mutable_data(tmp_path):
    paths = resolve_app_paths(
        platform="win32",
        home=tmp_path / "home",
        environ={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
    )

    assert paths.config_dir == tmp_path / "roaming" / "OpenAI-Free-Credit-Tracker"
    assert paths.data_dir == tmp_path / "local" / "OpenAI-Free-Credit-Tracker" / "Data"
    assert paths.cache_dir == tmp_path / "local" / "OpenAI-Free-Credit-Tracker" / "Cache"
    assert paths.log_dir == tmp_path / "local" / "OpenAI-Free-Credit-Tracker" / "Logs"
    assert paths.config_file.parent == paths.config_dir
    assert paths.update_cache_dir.parent == paths.cache_dir


def test_linux_paths_follow_xdg_and_support_absolute_managed_overrides(tmp_path):
    overridden_cache = tmp_path / "managed-cache"
    paths = resolve_app_paths(
        platform="linux",
        home=tmp_path / "home",
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            "OPENAI_CREDIT_TRACKER_CACHE_DIR": str(overridden_cache),
        },
    )

    assert paths.config_dir == tmp_path / "xdg-config" / "OpenAI-Free-Credit-Tracker"
    assert paths.data_dir == tmp_path / "xdg-data" / "OpenAI-Free-Credit-Tracker"
    assert paths.cache_dir == overridden_cache
    assert paths.log_dir == tmp_path / "xdg-state" / "OpenAI-Free-Credit-Tracker" / "log"


def test_relative_path_override_is_rejected():
    with pytest.raises(ValueError, match="must be absolute"):
        resolve_app_paths(
            platform="linux",
            home=Path("C:/users/test"),
            environ={"OPENAI_CREDIT_TRACKER_CONFIG_DIR": "repository/config"},
        )


def test_relative_platform_base_is_rejected_to_prevent_working_directory_writes(tmp_path):
    with pytest.raises(ValueError, match="XDG_CONFIG_HOME must be an absolute path"):
        resolve_app_paths(
            platform="linux",
            home=tmp_path,
            environ={"XDG_CONFIG_HOME": "relative-config"},
        )


def test_ensure_directories_creates_only_managed_directories(tmp_path):
    paths = resolve_app_paths(
        platform="linux",
        home=tmp_path,
        environ={},
        application_name="tracker-test",
    )

    paths.ensure_directories()

    assert all(
        path.is_dir()
        for path in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir)
    )
    assert not paths.config_file.exists()
