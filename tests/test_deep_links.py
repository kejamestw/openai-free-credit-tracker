import pytest

from quota_monitor.platform_adapters import (
    DeepLinkValidationError,
    NotificationMessage,
    build_deep_link,
    parse_deep_link,
)


PROFILE_ID = "prof_" + "a" * 32


def test_internal_and_custom_scheme_deep_links_normalize_to_allowlisted_route():
    internal = parse_deep_link(f"/dashboard?view=alerts&profile_id={PROFILE_ID}")
    custom = parse_deep_link(
        f"openai-free-credit-tracker://open/dashboard?profile_id={PROFILE_ID}"
    )

    assert internal.route == "/dashboard"
    assert internal.get("profile_id") == PROFILE_ID
    assert custom.as_internal_path() == f"/dashboard?profile_id={PROFILE_ID}"
    assert build_deep_link("/profiles", profile_id=PROFILE_ID, action="edit") == (
        f"/profiles?action=edit&profile_id={PROFILE_ID}"
    )
    NotificationMessage("title.key", "body.key", deep_link=custom.as_internal_path())


@pytest.mark.parametrize(
    "value",
    [
        "https://example.invalid/dashboard",
        "file:///etc/passwd",
        "/../../settings",
        "/%2e%2e/settings",
        "/dashboard?profile_id=bad",
        "/dashboard?view=summary&view=alerts",
        "/dashboard?url=https%3A%2F%2Fexample.invalid",
        "/unknown",
        "/dashboard#fragment",
        "openai-free-credit-tracker://open:invalid/dashboard",
        "/dashboard?profile_id=sk-admin-" + "x" * 12,
    ],
)
def test_deep_link_rejects_external_paths_traversal_duplicates_and_credentials(value):
    with pytest.raises(DeepLinkValidationError):
        parse_deep_link(value)
