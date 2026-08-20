import copy
import json

import pytest

from quota_monitor.catalog_schema import CatalogValidationError, validate_catalog
from quota_monitor.model_catalog import (
    clean_model_name,
    find_model,
    load_catalog,
    load_catalog_status,
    resource_root,
)


def test_clean_snapshot_date():
    assert clean_model_name("gpt-5.4-mini-2026-03-17") == "gpt-5.4-mini"


def test_alias_lookup():
    catalog = load_catalog()
    assert find_model("gpt-5.4-mini-2026-03-17", catalog)["group"] == "mini"


def test_resource_root_uses_pyinstaller_bundle_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("quota_monitor.model_catalog.sys._MEIPASS", str(tmp_path), raising=False)
    assert resource_root() == tmp_path.resolve()


def test_catalog_contains_verified_gpt_5_6_standard_prices():
    catalog = load_catalog()

    assert catalog["last_updated"] == "2026-08-19"
    assert find_model("gpt-5.6-terra", catalog)["pricing"] == {
        "input": 2,
        "cached_input": 0.2,
        "output": 12,
    }
    assert find_model("gpt-5.6-luna", catalog)["pricing"] == {
        "input": 0.2,
        "cached_input": 0.02,
        "output": 1.2,
    }


def test_catalog_v1_records_content_version_effective_dates_and_separate_sources():
    catalog = load_catalog()

    assert catalog["schema_version"] == 1
    assert catalog["catalog_version"] == "2026.08.19.1"
    assert catalog["effective_from"] <= catalog["last_verified"]
    assert catalog["sources"]["openai_pricing"]["kind"] == "first_party"
    assert catalog["sources"]["project_quota_policy"]["kind"] == "project_policy"
    assert validate_catalog(catalog) == 13


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["groups"]["mini"].update({"quota_source": "missing"}), "quota source"),
        (lambda data: data["groups"]["mini"]["models"][0]["pricing"].update({"input": -1}), "negative"),
        (
            lambda data: data["groups"]["mini"]["models"][0]["aliases"].append("gpt-5.6-sol"),
            "duplicate",
        ),
        (lambda data: data["groups"].update({"mystery": {}}), "unknown group"),
        (lambda data: data.update({"schema_version": 99}), "unsupported"),
    ],
)
def test_catalog_validator_rejects_unsafe_content(mutation, message):
    catalog = copy.deepcopy(load_catalog())
    mutation(catalog)

    with pytest.raises(CatalogValidationError, match=message):
        validate_catalog(catalog)


def test_invalid_external_catalog_uses_bundled_last_known_good(tmp_path):
    invalid = tmp_path / "models.json"
    invalid.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    result = load_catalog_status(invalid)

    assert result.used_fallback is True
    assert result.warning_code == "catalog_fallback"
    assert result.catalog["catalog_version"] == "2026.08.19.1"
