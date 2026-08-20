import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Formatter

import pytest

from quota_monitor.i18n import (
    DEFAULT_LOCALE_DIRECTORY,
    LocaleCatalog,
    LocaleError,
    TranslationFormatError,
    canonicalize_locale,
    format_number,
    format_usd,
    format_utc,
    find_unused_locale_keys,
    load_locale_directory,
    pseudo_localize,
    validate_locale_messages,
    validate_locale_usage,
)


def test_repository_locales_are_complete_and_have_matching_placeholders():
    catalogs = load_locale_directory()

    assert set(catalogs) == {"en", "zh-TW"}
    assert set(catalogs["en"]) == set(catalogs["zh-TW"])
    assert len(catalogs["en"]) >= 40


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("zh_TW", "zh-TW"),
        ("zh-Hant", "zh-TW"),
        ("en-US", "en"),
        ("fr-FR", "en"),
        (None, "en"),
    ],
)
def test_locale_resolution_has_deterministic_fallback(requested, expected):
    assert canonicalize_locale(requested, ["en", "zh-TW"]) == expected


def test_translation_uses_selected_locale_and_formats_named_placeholders():
    catalog = LocaleCatalog.from_directory("zh_TW")

    translated = catalog.translate(
        "alerts.threshold_label",
        parameters={"percent": 80},
    )

    assert "80%" in translated
    assert catalog.with_locale("en-US").translate("common.save") == "Save"


def test_missing_selected_key_falls_back_without_exposing_raw_key():
    catalog = LocaleCatalog(
        catalogs={
            "en": {"errors.offline": "Connection unavailable"},
            "zh-TW": {"common.save": "儲存"},
        },
        locale="zh-TW",
    )

    assert catalog.translate("errors.offline") == "Connection unavailable"
    assert catalog.translate("errors.not_registered") == "Not registered"


def test_missing_translation_parameters_fail_with_sanitized_error():
    catalog = LocaleCatalog.from_directory("en")

    with pytest.raises(TranslationFormatError) as caught:
        catalog.translate("alerts.threshold_label")

    assert "percent" in str(caught.value)


def test_locale_formatters_are_utc_and_locale_aware():
    instant = datetime(2026, 8, 9, 20, 5, tzinfo=timezone(timedelta(hours=8)))

    assert format_number(1234567, "en") == "1,234,567"
    assert format_usd(1234.5, "en") == "$1,234.50"
    assert format_usd(1234.5, "zh-TW") == "US$1,234.50"
    assert format_utc(instant, "en") == "2026-08-09 12:05 UTC"
    assert format_utc(instant, "zh-TW") == "2026年08月09日 12:05 UTC"


def test_pseudo_locale_preserves_placeholders():
    source = "Profile 雲端☁ — {name} used {percent}%"
    result = pseudo_localize(source)

    assert result.startswith("［") and result.endswith("］")
    assert result.count("{name}") == 1
    assert result.count("{percent}") == 1
    assert "雲端☁ —" in result
    assert len(result) >= len(source) + 6


def test_pseudo_locale_preserves_every_repository_placeholder_and_expands_text():
    catalogs = load_locale_directory()

    for message in catalogs["en"].values():
        pseudo = pseudo_localize(message)
        expected = {
            field_name
            for _literal, field_name, _spec, _conversion in Formatter().parse(message)
            if field_name is not None
        }
        actual = {
            field_name
            for _literal, field_name, _spec, _conversion in Formatter().parse(pseudo)
            if field_name is not None
        }
        assert actual == expected
        assert len(pseudo) > len(message)


def test_unicode_messages_and_matching_placeholders_validate():
    catalogs = {
        "en": {"greeting": "Hello 👋 — café {name}"},
        "zh-TW": {"greeting": "你好 👋 — café {name}"},
    }

    assert validate_locale_messages(catalogs) == (2, 1)


def test_unused_key_scan_honors_production_dynamic_namespaces(tmp_path):
    source = tmp_path / "web" / "app.js"
    source.parent.mkdir()
    source.write_text(
        "t('common.save'); t(`errors.${code}`); t(`update.state.${state}`);",
        encoding="utf-8",
    )
    catalogs = {
        "en": {
            "common.save": "Save",
            "common.dead": "Dead",
            "errors.offline": "Offline",
            "update.state.idle": "Idle",
        }
    }

    assert find_unused_locale_keys(catalogs, tmp_path) == ("common.dead",)


def test_repository_locale_keys_have_production_consumers():
    root = Path(__file__).resolve().parents[1]
    catalogs = load_locale_directory()

    assert validate_locale_usage(catalogs, root) == len(catalogs["en"])


def test_locale_validator_rejects_missing_keys_and_placeholder_drift(tmp_path):
    source = json.loads((DEFAULT_LOCALE_DIRECTORY / "en.json").read_text(encoding="utf-8"))
    (tmp_path / "en.json").write_text(json.dumps(source), encoding="utf-8")
    source["alerts"]["threshold_label"] = "用量達到門檻"
    (tmp_path / "zh-TW.json").write_text(
        json.dumps(source, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(LocaleError, match="placeholder mismatch"):
        load_locale_directory(tmp_path)


def test_validate_locales_script_succeeds_from_repository_root():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/validate_locales.py"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "locales valid" in result.stdout
