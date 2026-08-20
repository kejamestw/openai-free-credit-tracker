import pytest

from quota_monitor.semver import SemVer, SemVerError


def test_semver_uses_spec_precedence_instead_of_string_order():
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.10.0",
    ]

    parsed = [SemVer.parse(value) for value in ordered]

    assert parsed == sorted(reversed(parsed))
    assert SemVer.parse("1.10.0") > SemVer.parse("1.9.9")


def test_build_metadata_does_not_change_precedence_or_equality():
    assert SemVer.parse("1.2.3+build.1") == SemVer.parse("1.2.3+build.2")
    assert str(SemVer.parse("1.2.3-rc.1+sha.123")) == "1.2.3-rc.1+sha.123"


@pytest.mark.parametrize(
    "value",
    ["v1.2.3", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.0.0-01", "1.0.0+"],
)
def test_invalid_semver_is_rejected(value):
    with pytest.raises(SemVerError):
        SemVer.parse(value)
