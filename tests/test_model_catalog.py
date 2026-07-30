from quota_monitor.model_catalog import clean_model_name, find_model, load_catalog, resource_root


def test_clean_snapshot_date():
    assert clean_model_name("gpt-5.4-mini-2026-03-17") == "gpt-5.4-mini"


def test_alias_lookup():
    catalog = load_catalog()
    assert find_model("gpt-5.4-mini-2026-03-17", catalog)["group"] == "mini"


def test_resource_root_uses_pyinstaller_bundle_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("quota_monitor.model_catalog.sys._MEIPASS", str(tmp_path), raising=False)
    assert resource_root() == tmp_path.resolve()
