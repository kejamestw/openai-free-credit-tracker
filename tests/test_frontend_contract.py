import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.i18n_keys = set()
        self.ids = set()
        self.label_targets = set()
        self.external_urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "label" and values.get("for"):
            self.label_targets.add(values["for"])
        if values.get("data-i18n"):
            self.i18n_keys.add(values["data-i18n"])
        for name in ("href", "src"):
            value = values.get(name, "")
            if value.startswith(("http://", "https://", "//")):
                self.external_urls.append(value)


def flatten(document, prefix=""):
    result = {}
    for key, value in document.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def frontend_sources():
    return {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in (WEB / "index.html", WEB / "css" / "app.css", WEB / "js" / "app.js")
    }


def test_frontend_is_valid_utf8_without_previous_mojibake_markers():
    sources = frontend_sources()
    combined = "\n".join(sources.values())
    assert "�" not in combined
    assert not any(marker in combined for marker in ("銝餃", "嚗", "憿", "撠"))
    assert "看懂今日免費額度" in sources[Path("web/index.html")]


def test_frontend_has_no_remote_assets_or_browser_persistence():
    parser = FrontendParser()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")

    assert parser.external_urls == []
    for prohibited in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "serviceWorker",
        "eval(",
        "innerHTML",
    ):
        assert prohibited not in source


def test_primary_form_controls_have_explicit_labels_and_safe_key_behavior():
    parser = FrontendParser()
    source = (WEB / "index.html").read_text(encoding="utf-8")
    parser.feed(source)

    required_labeled_controls = {
        "profileSelector",
        "localeSelector",
        "key",
        "historyStart",
        "historyEnd",
        "historyProject",
        "settingLanguage",
        "requestTimeout",
        "monitoringInterval",
        "freshnessThreshold",
        "updateChannel",
        "retentionDays",
    }
    assert required_labeled_controls <= parser.label_targets
    assert 'id="key" type="password"' in source
    assert 'autocomplete="off"' in source
    assert 'aria-live="polite"' in source
    assert 'href="#mainContent"' in source


def test_all_static_and_literal_runtime_translation_keys_exist_in_both_locales():
    parser = FrontendParser()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    javascript = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    literal_calls = set(re.findall(r"\bt\('([^']+)'", javascript))
    required = parser.i18n_keys | literal_calls

    for locale in ("en", "zh-TW"):
        catalog = flatten(json.loads((ROOT / "locales" / f"{locale}.json").read_text(encoding="utf-8")))
        assert required - catalog.keys() == set()


def test_browser_uses_only_versioned_public_api_paths():
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    api_literals = re.findall(r"['`](/api/[^'`?$]+)", source)
    assert api_literals
    assert all(path.startswith("/api/v1/") for path in api_literals)


def test_responsive_accessibility_and_dark_mode_styles_are_present():
    source = (WEB / "css" / "app.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme:dark" in source
    assert "prefers-reduced-motion:reduce" in source
    assert "forced-colors:active" in source
    assert "focus-visible" in source
    assert "@media(max-width:680px)" in source


def test_history_trends_break_on_incomplete_days_and_share_the_project_filter():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    parser = FrontendParser()
    parser.feed(html)

    assert {
        "historyRange7", "historyRange30", "historyRange90", "historyRange365",
        "historyTrend", "trendSummary",
    } <= parser.ids
    assert "if (el('historyProject').value) params.set('project_key'" in source
    assert "renderHistoryTrend(data.records || [])" in source
    assert "record.completeness === 'complete'" in source
    assert "record.completeness === 'partial'" in source
    assert "record.completeness !== 'missing'" in source
    assert "trend-missing" in source
    assert 'role="img"' in html
    assert "createElementNS" in source


def test_retention_ui_is_preview_first_and_never_deletes_on_initialization():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    parser = FrontendParser()
    parser.feed(html)

    assert {"previewRetention", "applyRetention", "retentionStatus"} <= parser.ids
    assert "/api/v1/operations/retention/preview" in source
    assert "/api/v1/operations/retention/apply" in source
    assert "preview_token: preview.preview_token, confirm: true" in source
    initialize_body = source.split("async function initialize()", 1)[1]
    assert "applyRetention()" not in initialize_body


def test_config_reset_is_previewed_and_preserves_profile_and_unknown_fields():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    domain = (WEB / "js" / "domain.js").read_text(encoding="utf-8")
    parser = FrontendParser()
    parser.feed(html)

    assert {"previewConfigDefaults", "applyConfigDefaults", "configResetStatus"} <= parser.ids
    assert "defaultConfigDocument = response.defaults || null" in source
    assert "configResetPreview(configDocument, defaultConfigDocument)" in source
    assert "body: JSON.stringify(candidate)" in source
    assert "profiles.active_profile_id" not in domain.split(
        "const RESETTABLE_CONFIG_FIELDS", 1
    )[1].split("]);", 1)[0]
    assert "const candidate = JSON.parse(JSON.stringify(current))" in domain


def test_notification_test_and_sanitized_history_are_exposed_without_profile_labels():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    parser = FrontendParser()
    parser.feed(html)

    assert {
        "testNotification", "notificationHistoryRows", "notificationHistoryStatus"
    } <= parser.ids
    assert "/api/v1/notifications/test" in source
    assert "/api/v1/alerts/history" in source
    test_body = source.split("async function testNotification()", 1)[1].split(
        "async function saveAlert", 1
    )[0]
    assert "display_name" not in test_body
    assert "admin_key" not in test_body


def test_profile_delete_discloses_scope_and_requires_two_confirmations():
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    body = source.split("async function deleteProfile(profile)", 1)[1].split(
        "async function deleteCredential", 1
    )[0]

    assert "profile.delete_scope_confirmation" in body
    assert "profile.delete_final_confirmation" in body
    assert body.count("globalThis.confirm(") == 2


def test_cache_write_estimate_incomplete_warning_has_priority():
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    domain = (WEB / "js" / "domain.js").read_text(encoding="utf-8")

    assert "t(costNoteKey(data.usage, data.costs))" in source
    function = domain.split("function costNoteKey", 1)[1].split("function", 1)[0]
    assert "list_price_estimate_incomplete === true" in function
    assert function.index("list_price_estimate_incomplete") < function.index("costs.error")


def test_validated_deep_link_state_is_applied_after_profiles_and_before_view_load():
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    domain = (WEB / "js" / "domain.js").read_text(encoding="utf-8")

    assert "parseNavigationTarget" in source
    assert "NAVIGATION_RULES" in domain
    assert "profile_id: PROFILE_ID_PATTERN" in domain
    assert "project_key: PROJECT_KEY_PATTERN" in domain
    assert "view: new Set(['summary', 'projects', 'history', 'alerts'])" in domain
    assert "await applyInitialNavigation(profiles);" in source
    assert source.index("await applyInitialNavigation(profiles);") > source.index("loadProfiles(),")
    assert "profiles.find(profile => profile.profile_id === navigation.profileId)" in source
    navigation_body = source.split("async function applyInitialNavigation(profiles)", 1)[1].split(
        "async function createProfile", 1
    )[0]
    assert "await activateProfile(navigation.profileId)" in navigation_body
    assert "if (!activated || activeProfileId !== navigation.profileId) return;" in navigation_body
    assert navigation_body.index("await activateProfile(navigation.profileId)") < navigation_body.index(
        "showView(navigation.view)"
    )
    assert "/activate`" in source
    activation_body = source.split("async function activateProfile(profileId)", 1)[1].split(
        "async function updateProfile", 1
    )[0]
    assert "return true;" in activation_body
    assert "return false;" in activation_body
    assert "optionExists" in source
    assert "el('historyStart').value = navigation.utcDay" in source


def test_update_actions_follow_server_capabilities_and_fail_closed_packages_are_explained():
    source = (WEB / "js" / "app.js").read_text(encoding="utf-8")

    assert "snapshot?.can_consent_install || snapshot?.can_install" in source
    assert "el('resumeUpdate').hidden = !snapshot?.can_resume" in source
    assert "snapshot.installation_available === false" in source
    assert "update.installation_unavailable" in source
