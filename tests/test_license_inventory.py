from email.message import Message

from scripts.inventory_licenses import build_inventory


class FakeDistribution:
    def __init__(self, name: str, version: str, *, license_expression: str = ""):
        item = Message()
        item["Name"] = name
        if license_expression:
            item["License-Expression"] = license_expression
        self.metadata = item
        self.version = version


def test_license_inventory_is_sorted_and_reports_unknowns_without_paths():
    payload = build_inventory(
        [
            FakeDistribution("Zulu", "2", license_expression="MIT"),
            FakeDistribution("Alpha", "1"),
        ]
    )
    assert [item["name"] for item in payload["packages"]] == ["Alpha", "Zulu"]
    assert payload["packages"][1]["licenses"] == ["MIT"]
    assert payload["summary"]["unknown_license_packages"] == ["Alpha"]
    assert "path" not in str(payload).lower()
