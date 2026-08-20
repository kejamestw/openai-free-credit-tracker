import pytest

from quota_monitor.canonical_json import CanonicalizationError, canonicalize_json


def test_canonical_json_sorts_keys_and_uses_minimal_utf8_json():
    value = {"b": 1, "a": "line\n雪", "nested": {"z": None, "a": True}}

    assert canonicalize_json(value) == (
        '{"a":"line\\n雪","b":1,"nested":{"a":true,"z":null}}'.encode("utf-8")
    )


def test_canonical_json_uses_utf16_key_order_required_by_jcs():
    # U+10000 is represented by D800 DC00 and therefore sorts before U+E000.
    assert canonicalize_json({"\ue000": 1, "\U00010000": 2}) == (
        '{"𐀀":2,"":1}'.encode("utf-8")
    )


@pytest.mark.parametrize("value", [1.5, float("nan"), 9_007_199_254_740_992, "\ud800"])
def test_canonical_json_rejects_values_outside_the_manifest_subset(value):
    with pytest.raises(CanonicalizationError):
        canonicalize_json(value)
