from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_page_exposes_version_utc_disclaimer_and_live_state():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="appVersion"' in html
    assert 'id="dataWindow"' in html
    assert "UTC" in html
    assert "非官方" in html
    assert 'id="status"' in html
    assert 'aria-live="polite"' in html
    assert "0.1.0" not in html


def test_app_implements_all_five_query_states_and_accessible_tooltips():
    source = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
    for state in ("initial", "loading", "success", "partial", "failure"):
        assert f"'{state}'" in source
    assert 'role="tooltip"' in source
    assert "tabindex=\"0\"" in source
