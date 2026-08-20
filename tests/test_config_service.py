import json
import os

import pytest

from quota_monitor.config_service import (
    AppConfig,
    ConfigLoadSource,
    ConfigService,
    ConfigValidationError,
    ConfigWriteError,
    HistorySettings,
    MonitoringSettings,
    NetworkSettings,
    ProfilesSettings,
    SensitiveConfigError,
    StartupSettings,
    UISettings,
    UnknownFieldPolicy,
    UpdateSettings,
    config_to_dict,
    default_config,
    default_update_channel,
    parse_config,
)
from quota_monitor.platform_paths import AppPaths


def make_paths(tmp_path):
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )


def configured(timeout=90, channel="beta"):
    return AppConfig(
        ui=UISettings(language="zh-TW", open_browser_on_start=False),
        network=NetworkSettings(request_timeout_seconds=timeout),
        updates=UpdateSettings(channel=channel, check_on_start=False),
    )


def test_default_schema_matches_the_v1_contract():
    assert config_to_dict(default_config()) == {
        "schema_version": 1,
        "ui": {"language": "zh-TW", "open_browser_on_start": True},
        "network": {"request_timeout_seconds": 45},
        "updates": {"channel": default_update_channel(), "check_on_start": True},
        "history": {"retention_days": None},
        "monitoring": {
            "enabled": False,
            "interval_seconds": 900,
            "freshness_threshold_seconds": 1800,
        },
        "profiles": {"active_profile_id": None},
        "startup": {"enabled": False},
    }


def test_parse_config_preserves_unknown_fields_by_default_and_supports_other_policies():
    document = config_to_dict(default_config())
    document["future"] = {"enabled": True}
    document["ui"]["density"] = "compact"

    parsed = parse_config(document)
    assert config_to_dict(parsed)["future"] == {"enabled": True}
    assert config_to_dict(parsed)["ui"]["density"] == "compact"
    with pytest.raises(ConfigValidationError, match="unknown fields: future"):
        parse_config(document, unknown_fields=UnknownFieldPolicy.REJECT)

    assert parse_config(
        document, unknown_fields=UnknownFieldPolicy.IGNORE
    ) == default_config()


def test_missing_optional_sections_and_fields_receive_defaults():
    assert parse_config({"schema_version": 1}) == default_config()
    assert parse_config(
        {"schema_version": 1, "network": {"request_timeout_seconds": 30}}
    ) == AppConfig(network=NetworkSettings(request_timeout_seconds=30))


def test_preserved_fields_survive_service_round_trip_without_being_executed(tmp_path):
    document = {
        "schema_version": 1,
        "history": {"retention_days": 90, "future_cleanup_mode": "manual"},
    }
    config = parse_config(document)
    service = ConfigService(make_paths(tmp_path))

    service.save(config)
    loaded = service.load().config

    assert config_to_dict(loaded)["history"] == {
        "retention_days": 90,
        "future_cleanup_mode": "manual",
    }
    assert loaded.ui == default_config().ui


def test_non_standard_json_numbers_recover_safely(tmp_path):
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text(
        '{"schema_version":1,"future":{"value":NaN}}', encoding="utf-8"
    )

    result = ConfigService(paths).load()

    assert result.source is ConfigLoadSource.DEFAULTS
    assert result.warning is not None


def test_secret_bearing_unknown_config_is_preserved_for_diagnosis_but_not_loaded(tmp_path):
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    fake_secret = "sk-" + "admin-" + "abcdefghijklmnop"
    paths.config_file.write_text(
        json.dumps({"schema_version": 1, "future": {"api_key": fake_secret}}),
        encoding="utf-8",
    )

    result = ConfigService(paths).load()

    assert result.source is ConfigLoadSource.DEFAULTS
    assert result.preserved_corrupt_path is not None
    assert "api_key" not in config_to_dict(result.config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("timeout", 4, "between 5 and 300"),
        ("timeout", 301, "between 5 and 300"),
        ("timeout", True, "must be an integer"),
        ("channel", "nightly", "must be one of"),
        ("browser", 1, "must be a boolean"),
    ],
)
def test_config_validation_rejects_invalid_types_and_ranges(field, value, message):
    document = config_to_dict(default_config())
    if field == "timeout":
        document["network"]["request_timeout_seconds"] = value
    elif field == "channel":
        document["updates"]["channel"] = value
    else:
        document["ui"]["open_browser_on_start"] = value

    with pytest.raises(ConfigValidationError, match=message):
        parse_config(document)


def test_save_and_load_round_trip_without_sensitive_fields(tmp_path):
    paths = make_paths(tmp_path)
    service = ConfigService(paths)

    service.save(configured())
    result = service.load()

    assert result.config == configured()
    assert result.source is ConfigLoadSource.FILE
    stored = json.loads(paths.config_file.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert "key" not in paths.config_file.read_text(encoding="utf-8").lower()


def test_overwrite_keeps_last_valid_backup_and_corrupt_load_uses_it(tmp_path):
    paths = make_paths(tmp_path)
    service = ConfigService(paths)
    old_config = configured(timeout=60)
    new_config = configured(timeout=120)
    service.save(old_config)
    service.save(new_config)
    corrupt_bytes = b'{"schema_version": 1, "ui": '
    paths.config_file.write_bytes(corrupt_bytes)

    result = service.load()

    assert result.config == old_config
    assert result.source is ConfigLoadSource.BACKUP
    assert result.warning and "last valid backup" in result.warning
    assert result.preserved_corrupt_path is not None
    assert result.preserved_corrupt_path.read_bytes() == corrupt_bytes
    assert paths.config_file.read_bytes() == corrupt_bytes


def test_corrupt_config_without_backup_recovers_to_defaults(tmp_path):
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("{", encoding="utf-8")

    result = ConfigService(paths).load()

    assert result.config == default_config()
    assert result.source is ConfigLoadSource.DEFAULTS
    assert result.warning and "safe defaults" in result.warning


def test_atomic_replace_failure_preserves_the_old_complete_config(monkeypatch, tmp_path):
    paths = make_paths(tmp_path)
    service = ConfigService(paths)
    old_config = configured(timeout=60)
    service.save(old_config)
    original_replace = os.replace

    def fail_config_replace(source, destination):
        if destination == paths.config_file:
            raise OSError("simulated replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr("quota_monitor.config_service.os.replace", fail_config_replace)
    with pytest.raises(ConfigWriteError, match="could not write"):
        service.save(configured(timeout=120))

    assert parse_config(json.loads(paths.config_file.read_text(encoding="utf-8"))) == old_config
    assert list(paths.config_dir.glob("*.tmp")) == []


def test_duplicate_json_field_is_rejected_and_does_not_block_startup(tmp_path):
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text(
        '{"schema_version":1,"schema_version":1,"ui":{},"network":{},"updates":{}}',
        encoding="utf-8",
    )

    result = ConfigService(paths).load()

    assert result.source is ConfigLoadSource.DEFAULTS
    assert result.warning is not None


@pytest.mark.parametrize(
    "candidate",
    [
        {"nested": {"admin_api_key": "not-even-a-real-secret"}},
        {"token": "sk-" + "admin-" + "1234567890abcdefghijklmnop"},
        {"authorization_header": "Bearer abcdefghijklmnop"},
        {"project_display_name": "private project"},
    ],
)
def test_safe_export_rejects_sensitive_keys_and_values(tmp_path, candidate):
    service = ConfigService(make_paths(tmp_path))

    with pytest.raises(SensitiveConfigError):
        service.export_json(candidate=candidate)


def test_safe_export_of_schema_config_is_stable_and_round_trips(tmp_path):
    exported = ConfigService(make_paths(tmp_path)).export_json(configured())

    assert parse_config(json.loads(exported)) == configured()
    assert exported.endswith("\n")


def test_safe_export_rejects_non_json_numbers(tmp_path):
    with pytest.raises(ConfigValidationError, match="JSON values"):
        ConfigService(make_paths(tmp_path)).export_json(candidate={"ratio": float("nan")})


def test_all_final_v1_optional_sections_round_trip(tmp_path):
    config = AppConfig(
        history=HistorySettings(retention_days=3650),
        monitoring=MonitoringSettings(
            enabled=True,
            interval_seconds=300,
            freshness_threshold_seconds=600,
        ),
        profiles=ProfilesSettings(active_profile_id="prof_" + "a" * 32),
        startup=StartupSettings(enabled=True),
    )
    service = ConfigService(make_paths(tmp_path))

    service.save(config)

    assert service.load().config == config
    assert parse_config(config_to_dict(config)) == config


@pytest.mark.parametrize("retention_days", [0, 3651, True, "90"])
def test_history_retention_range_and_type_are_validated(retention_days):
    document = config_to_dict(default_config())
    document["history"]["retention_days"] = retention_days

    with pytest.raises(ConfigValidationError, match="history.retention_days"):
        parse_config(document)


@pytest.mark.parametrize(
    ("interval", "freshness", "message"),
    [
        (299, 600, "at least 300"),
        (300, 299, "greater than or equal"),
        (True, 600, "must be an integer"),
    ],
)
def test_monitoring_interval_and_freshness_are_validated(interval, freshness, message):
    document = config_to_dict(default_config())
    document["monitoring"].update(
        interval_seconds=interval,
        freshness_threshold_seconds=freshness,
    )

    with pytest.raises(ConfigValidationError, match=message):
        parse_config(document)


def test_nullable_retention_and_active_profile_defaults_are_explicit():
    parsed = parse_config({"schema_version": 1})

    assert parsed.history.retention_days is None
    assert parsed.profiles.active_profile_id is None
    assert parsed.monitoring.enabled is False
    assert parsed.startup.enabled is False
