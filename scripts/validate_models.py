import json
from pathlib import Path

from quota_monitor.catalog_schema import validate_catalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "models.json"
def main() -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    identifiers = validate_catalog(data)
    print(f"models.json valid: {identifiers} unique model identifiers")


if __name__ == "__main__":
    main()
