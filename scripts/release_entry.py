"""PyInstaller entry point with a non-destructive packaged lifecycle check."""

from __future__ import annotations

import importlib
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from quota_monitor.app import main, validate_resources
from quota_monitor.database import DatabaseService
from quota_monitor.export_service import export_csv, export_json
from quota_monitor.i18n import load_locale_directory
from quota_monitor.model_catalog import resource_path
from quota_monitor.update_runtime import load_product_update_trust
from quota_monitor.version import __version__


SELF_TEST_FLAG = "--packaged-self-test"
IMPORT_SMOKE_FLAG = "--packaged-import-smoke"
DESKTOP_RUNTIME_IMPORTS = {
    "win32": (
        "desktop_notifier",
        "desktop_notifier.backends.winrt",
        "pystray",
        "pystray._win32",
        "PIL.Image",
        "PIL.ImageDraw",
    ),
    "darwin": (
        "desktop_notifier",
        "desktop_notifier.backends.macos",
        "desktop_notifier.backends.macos_support",
        "pystray",
        "pystray._darwin",
        "PIL.Image",
        "PIL.ImageDraw",
        "rubicon.objc.eventloop",
    ),
    "linux": (
        "desktop_notifier",
        "desktop_notifier.backends.dbus",
        "pystray",
        "pystray._xorg",
        "PIL.Image",
        "PIL.ImageDraw",
        "dbus_fast",
    ),
}


def validate_desktop_runtime_imports(
    platform_name: str | None = None,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> tuple[str, ...]:
    """Import frozen desktop dependencies without constructing native objects."""

    platform_name = sys.platform if platform_name is None else platform_name
    try:
        required = DESKTOP_RUNTIME_IMPORTS[platform_name]
    except KeyError:
        raise RuntimeError(f"unsupported packaged desktop platform: {platform_name}") from None
    for module_name in required:
        importer(module_name)
    return required


def packaged_import_smoke() -> bool:
    """Import deferred desktop modules without starting native services or writing state."""

    validate_resources()
    load_locale_directory(resource_path("locales"))
    if getattr(sys, "frozen", False):
        validate_desktop_runtime_imports()
    trust_path = resource_path("data", "update-trust.json")
    trust_present = trust_path.is_file()
    if trust_present:
        load_product_update_trust(trust_path)
    state = "present" if trust_present else "absent"
    print(f"OpenAI Free Credit Tracker {__version__} packaged import smoke passed; update trust {state}")
    return trust_present


def packaged_self_test() -> None:
    """Exercise resources, SQLite, exports, and handle cleanup inside a temp dir."""

    packaged_import_smoke()
    with tempfile.TemporaryDirectory(
        prefix="quota-monitor-package-", ignore_cleanup_errors=True
    ) as temporary:
        root = Path(temporary)
        database_path = root / "history.sqlite3"
        csv_path = root / "history.csv"
        json_path = root / "history.json"

        database = DatabaseService(database_path)
        if database.schema_version < 1 or not database.check_integrity(full=True).ok:
            raise RuntimeError("packaged SQLite lifecycle check failed")
        export_csv(database, csv_path, 0, 1)
        export_json(database, json_path, 0, 1, generated_at="1970-01-01T00:00:00Z")

        if not csv_path.read_bytes().startswith(b"schema_version,"):
            raise RuntimeError("packaged CSV export check failed")
        if b'"schema_version": 1' not in json_path.read_bytes():
            raise RuntimeError("packaged JSON export check failed")

        # Renaming and deleting proves no SQLite/export handle leaked on Windows.
        renamed_database = root / "closed.sqlite3"
        database_path.replace(renamed_database)
        renamed_database.unlink()
        csv_path.unlink()
        json_path.unlink()
        for sqlite_sidecar in root.glob("history.sqlite3-*"):
            sqlite_sidecar.unlink()

        # Cleanup is performed here as part of the check; the context manager's
        # ignore flag only protects against a short-lived OS/AV directory race.
        if any(root.iterdir()):
            raise RuntimeError("packaged lifecycle left temporary files open")

    print(f"OpenAI Free Credit Tracker {__version__} packaged self-test passed")


def entrypoint(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == [IMPORT_SMOKE_FLAG]:
        packaged_import_smoke()
        return
    if arguments == [SELF_TEST_FLAG]:
        packaged_self_test()
        return
    main(arguments)


if __name__ == "__main__":
    entrypoint()
