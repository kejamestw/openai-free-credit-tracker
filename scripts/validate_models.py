import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "models.json"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"


def validate_catalog(data: dict) -> int:
    assert data["schema_version"] == 1
    assert data["currency"] == "USD"
    assert data["price_unit"] == "per_1m_tokens"
    assert date.fromisoformat(data["last_updated"]) <= date.today()

    seen: set[str] = set()
    for group in data["groups"].values():
        assert isinstance(group["label"], str) and group["label"].strip()
        assert group["daily_quota_tier_1_2"] > 0
        for model in group["models"]:
            identifiers = [model["id"], *model.get("aliases", [])]
            assert all(isinstance(identifier, str) and identifier for identifier in identifiers)
            assert not seen.intersection(identifiers)
            seen.update(identifiers)
            assert model["eligible"] is True
            assert model["source"] == PRICING_SOURCE
            pricing = model["pricing"]
            assert set(pricing) == {"input", "cached_input", "output"}
            assert all(
                isinstance(pricing[key], (int, float)) and pricing[key] >= 0
                for key in ("input", "cached_input", "output")
            )
    return len(seen)


def main() -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    identifiers = validate_catalog(data)
    print(f"models.json valid: {identifiers} unique model identifiers")


if __name__ == "__main__":
    main()
