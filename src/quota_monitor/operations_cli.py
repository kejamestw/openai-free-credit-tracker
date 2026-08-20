"""Offline-safe maintenance commands for the local history database."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config_service import ConfigService
from .database import DatabaseService
from .export_service import atomic_write
from .model_catalog import load_catalog, resource_path
from .openai_client import OpenAIAdminClient
from .platform_adapters import (
    FileInstanceLock,
    create_native_credential_store,
    detect_platform_family,
)
from .platform_paths import AppPaths
from .runtime import OpenAIUsageCredentialVerifier, RuntimeRequestError, RuntimeServices
from .upstream_adapter import ProjectKeyDeriver
from .update_runtime import build_product_update_runtime
from .version import __version__


DATABASE_FILENAME = "history.sqlite3"
RUNTIME_LOCK_FILENAME = "runtime.lock"
PROJECT_KEY_SECRET_FILENAME = "project-key.secret"


def add_operations_subparsers(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="operation", title="operations")

    backup = subparsers.add_parser("backup", help="create a verified SQLite backup")
    backup.add_argument("--output", required=True, type=Path)

    restore = subparsers.add_parser("restore", help="restore a verified backup while offline")
    restore.add_argument("--source", required=True, type=Path)
    restore.add_argument(
        "--confirm-restore",
        action="store_true",
        help="confirm replacement of the active history database",
    )

    integrity = subparsers.add_parser("integrity", help="check SQLite integrity")
    integrity.add_argument("--full", action="store_true")

    export = subparsers.add_parser("export", help="export profile-scoped history")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--format", required=True, choices=("csv", "json"))
    export.add_argument("--start-utc", required=True, type=int)
    export.add_argument("--end-utc", required=True, type=int)
    export.add_argument("--profile-id")
    export.add_argument("--project-key")
    export.add_argument(
        "--project-id-policy", choices=("mask", "exclude"), default="mask"
    )


def build_data_runtime(paths: AppPaths) -> RuntimeServices:
    database = DatabaseService(paths.data_dir / DATABASE_FILENAME)
    credential_store = create_native_credential_store(detect_platform_family())
    config_service = ConfigService(paths)
    configured_channel = config_service.load().config.updates.channel
    update_runtime = build_product_update_runtime(
        paths=paths,
        channel=configured_channel,
        current_version=__version__,
        trust_path=resource_path("data", "update-trust.json"),
    )
    return RuntimeServices(
        paths=paths,
        config_service=config_service,
        update_runtime=update_runtime,
        database=database,
        credential_store=credential_store,
        credential_verifier=OpenAIUsageCredentialVerifier(),
        admin_client_factory=lambda key, timeout: OpenAIAdminClient(key, timeout=timeout),
        project_keys=load_or_create_project_keys(paths.data_dir / PROJECT_KEY_SECRET_FILENAME),
        catalog=load_catalog(),
    )


def load_or_create_project_keys(path: Path) -> ProjectKeyDeriver:
    secret_path = Path(path).resolve()
    secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        secret = secret_path.read_bytes()
    except FileNotFoundError:
        secret = os.urandom(32)
        try:
            descriptor = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            secret = secret_path.read_bytes()
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
    if len(secret) != 32:
        raise RuntimeError("project pseudonymization secret has an invalid length")
    return ProjectKeyDeriver(secret)


def run_operation(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.operation == "restore" and not args.confirm_restore:
        print("restore requires --confirm-restore", file=sys.stderr)
        return 2

    lock: FileInstanceLock | None = None
    if args.operation == "restore":
        lock = FileInstanceLock((paths.data_dir / RUNTIME_LOCK_FILENAME).resolve())
        if not lock.acquire():
            print("restore requires the dashboard and other operations to be stopped", file=sys.stderr)
            return 3

    try:
        runtime = build_data_runtime(paths)
        database = runtime.database
        assert database is not None
        if args.operation == "backup":
            output = database.backup_to(args.output)
            print(output)
            return 0
        if args.operation == "restore":
            output = database.replace_from_backup(args.source)
            print(output)
            return 0
        if args.operation == "integrity":
            print(json.dumps(runtime.integrity(full=args.full), sort_keys=True))
            return 0
        if args.operation == "export":
            body, _media_type, _filename = runtime.export_bytes(
                format=args.format,
                start_utc=args.start_utc,
                end_utc=args.end_utc,
                profile_id=args.profile_id,
                project_key=args.project_key,
                project_id_policy=args.project_id_policy,
            )
            print(atomic_write(args.output, body))
            return 0
        raise ValueError("unknown operation")
    except RuntimeRequestError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as error:
        print(f"operation_failed: {error}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.release()
