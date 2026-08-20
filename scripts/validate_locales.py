"""Validate locale key and placeholder completeness for release checks."""

from __future__ import annotations

import argparse
from pathlib import Path

from quota_monitor.i18n import LocaleError, load_locale_directory, validate_locale_usage


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRECTORY = ROOT / "locales"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=DEFAULT_DIRECTORY,
        help="directory containing locale JSON files",
    )
    arguments = parser.parse_args()
    try:
        catalogs = load_locale_directory(arguments.directory)
        validate_locale_usage(catalogs, ROOT)
    except LocaleError as exc:
        raise SystemExit(f"locale validation failed: {exc}") from None
    key_count = len(catalogs["en"])
    print(f"locales valid: {len(catalogs)} locales; {key_count} keys each")


if __name__ == "__main__":
    main()
