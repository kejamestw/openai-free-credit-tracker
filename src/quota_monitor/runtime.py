"""Application composition context shared by the CLI and local HTTP server."""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .alert_storage import SQLiteAlertState
from .alerts import AlertEvent, AlertRule

from .config_service import (
    AppConfig,
    ConfigLoadResult,
    ConfigLoadSource,
    ConfigService,
    UnknownFieldPolicy,
    config_to_dict,
    default_config,
    parse_config,
    validate_config,
)
from .platform_paths import AppPaths, resolve_app_paths
from .database import DatabaseService, epoch_to_utc_text
from .export_service import build_export_records, render_csv, render_json
from .model_catalog import load_catalog
from .openai_client import OpenAIClientError
from .platform_adapters import (
    AdapterUnavailableError,
    CredentialNotFoundError,
    CredentialStore,
    NotificationAdapter,
    NotificationMessage,
    StartupAdapter,
)
from .profiles import (
    DuplicateProfileError,
    ProfileHasDataError,
    ProfileId,
    ProfileNotFoundError,
    ProfileService,
    SQLiteProfileRepository,
    new_profile_id,
)
from .sync_service import HistoryOperations, RetentionPreview, UsageSyncService
from .upstream_adapter import AdminUsageClient, ProjectKeyDeriver
from .update_manifest import (
    ManifestFetcher,
    UpdateChecker,
    UpdateCheckResult,
)
from .update_runtime import UpdateRuntimeError, UpdateRuntimeService


RESTART_REQUIRED_CONFIG_FIELDS = frozenset(
    {
        "updates.channel",
        "monitoring.enabled",
        "monitoring.interval_seconds",
        "monitoring.freshness_threshold_seconds",
        "profiles.active_profile_id",
    }
)

_ALERT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_PROJECT_KEY = re.compile(r"(?:all|unattributed|project-[0-9a-f]{24})\Z", re.ASCII)
_MANAGED_BACKUP = re.compile(r"history-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.sqlite3\Z", re.ASCII)
_RETENTION_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,64}\Z", re.ASCII)


class RuntimeCapabilityUnavailable(RuntimeError):
    def __init__(self, capability: str):
        super().__init__(f"runtime capability is unavailable: {capability}")
        self.capability = capability


class RuntimeApplyError(RuntimeError):
    pass


class RuntimeRequestError(RuntimeError):
    """Safe, stable failure returned by versioned API and operations clients."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.params = dict(params or {})


@dataclass(frozen=True)
class CredentialVerification:
    """Result of an injected capability check; never contains credential material."""

    capability_verified: bool
    authoritative_opaque_identity: str | None = None


class CredentialVerifier(Protocol):
    def verify(self, client: AdminUsageClient) -> CredentialVerification: ...


AdminClientFactory = Callable[[str, int], AdminUsageClient]


class OpenAIUsageCredentialVerifier:
    """Verify organization-usage access without inventing organization identity."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, client: AdminUsageClient) -> CredentialVerification:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("credential verifier clock must return an aware datetime")
        now = now.astimezone(timezone.utc)
        end = int(now.timestamp())
        client.get(
            "/organization/usage/completions",
            {
                "start_time": max(0, end - 86_400),
                "end_time": end,
                "bucket_width": "1d",
                "limit": 1,
            },
        )
        # The usage endpoint proves capability. It does not provide an
        # authoritative organization identity, so none is synthesized here.
        return CredentialVerification(True)


class UpdateCheckService(Protocol):
    def check(self) -> UpdateCheckResult: ...


@dataclass(frozen=True)
class RemoteUpdateCheckService:
    """Bind the pure UpdateChecker to a configured manifest source."""

    checker: UpdateChecker
    manifest_url: str
    fetcher: ManifestFetcher
    timeout_seconds: float = 10.0

    def check(self) -> UpdateCheckResult:
        return self.checker.check_remote(
            self.manifest_url,
            fetcher=self.fetcher,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class ConfigUpdateResult:
    config: AppConfig
    restart_required: bool
    restart_required_fields: tuple[str, ...]
    applied_fields: tuple[str, ...] = ()


class RuntimeServices:
    """Thread-safe owner of runtime configuration and optional capabilities."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        config_service: ConfigService | None = None,
        update_checker: UpdateCheckService | None = None,
        update_runtime: UpdateRuntimeService | None = None,
        startup_adapter: StartupAdapter | None = None,
        initial_config: AppConfig | None = None,
        database: DatabaseService | None = None,
        profile_service: ProfileService | None = None,
        credential_store: CredentialStore | None = None,
        credential_verifier: CredentialVerifier | None = None,
        admin_client_factory: AdminClientFactory | None = None,
        project_keys: ProjectKeyDeriver | None = None,
        alert_state: SQLiteAlertState | None = None,
        notification_adapter: NotificationAdapter | None = None,
        catalog: dict | None = None,
    ) -> None:
        if paths is None:
            paths = config_service.paths if config_service is not None else resolve_app_paths()
        if config_service is not None and config_service.paths != paths:
            raise ValueError("config_service paths do not match runtime paths")
        self.paths = paths
        self.config_service = config_service or ConfigService(paths)
        if update_checker is not None and update_runtime is not None:
            raise ValueError("update_checker and update_runtime cannot both be configured")
        self.update_checker = update_checker
        self.update_runtime = update_runtime
        self.startup_adapter = startup_adapter
        self.database = database
        self.profile_service = profile_service or (
            ProfileService(SQLiteProfileRepository(database)) if database is not None else None
        )
        if profile_service is not None and database is None:
            raise ValueError("profile_service requires database")
        self.credential_store = credential_store
        self.credential_verifier = credential_verifier
        self.admin_client_factory = admin_client_factory
        self.project_keys = project_keys
        self.alert_state = alert_state or (
            SQLiteAlertState(database) if database is not None else None
        )
        self.notification_adapter = notification_adapter
        self.catalog = catalog or load_catalog()
        self._lock = threading.RLock()
        self._data_lock = threading.RLock()
        self._profile_generation = 0
        self._retention_previews: dict[str, RetentionPreview] = {}
        self._config_result = (
            ConfigLoadResult(
                config=validate_config(initial_config),
                source=ConfigLoadSource.DEFAULTS,
                config_path=paths.config_file,
            )
            if initial_config is not None
            else self.config_service.load()
        )

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config_result.config

    @property
    def config_result(self) -> ConfigLoadResult:
        with self._lock:
            return self._config_result

    def config_response(self) -> dict[str, Any]:
        with self._lock:
            result = self._config_result
            return {
                "config": config_to_dict(result.config),
                "defaults": config_to_dict(default_config()),
                "config_path": str(result.config_path),
                "load_source": result.source.value,
                "warning": result.warning,
                "restart_required": False,
                "restart_required_fields": [],
            }

    def replace_config(self, document: Mapping[str, Any]) -> ConfigUpdateResult:
        """Validate a complete API document, persist it, and publish it atomically."""

        # Preserve forward-compatible optional fields so a GET -> PUT round trip
        # cannot reject fields this binary already loaded. ConfigService.save
        # still performs the recursive secret scan before persistence.
        config = parse_config(document, unknown_fields=UnknownFieldPolicy.PRESERVE)
        with self._lock:
            previous_active = self._config_result.config.profiles.active_profile_id
            next_active = config.profiles.active_profile_id
            if next_active is not None and next_active != previous_active and self.profile_service is not None:
                profile = self._get_profile(next_active)
                if not profile.enabled:
                    raise RuntimeRequestError(409, "profile_disabled", "The profile is disabled.")
            previous = config_to_dict(self._config_result.config)
            current = config_to_dict(config)
            changed_restart_fields = tuple(
                sorted(
                    field
                    for field in RESTART_REQUIRED_CONFIG_FIELDS
                    if _nested_value(previous, field) != _nested_value(current, field)
                )
            )
            startup_changed = (
                self._config_result.config.startup.enabled != config.startup.enabled
            )
            applied_fields: tuple[str, ...] = ()
            if startup_changed:
                self._apply_startup(config.startup.enabled)
                applied_fields = ("startup.enabled",)
            try:
                self.config_service.save(config)
            except Exception:
                if startup_changed:
                    self._rollback_startup(self._config_result.config.startup.enabled)
                raise
            self._config_result = ConfigLoadResult(
                config=config,
                source=ConfigLoadSource.FILE,
                config_path=self.paths.config_file,
            )
            if previous_active != next_active:
                self._profile_generation += 1
            return ConfigUpdateResult(
                config=config,
                restart_required=bool(changed_restart_fields),
                restart_required_fields=changed_restart_fields,
                applied_fields=applied_fields,
            )

    def check_for_updates(self, *, prepare: bool = True) -> UpdateCheckResult | None:
        runtime = self.update_runtime
        if runtime is not None:
            try:
                return runtime.check(prepare=prepare)
            except UpdateRuntimeError as error:
                raise RuntimeRequestError(
                    error.status, error.code, error.message
                ) from None
        checker = self.update_checker
        if checker is None:
            return None
        return checker.check()

    def update_status(self) -> dict[str, Any] | None:
        runtime = self.update_runtime
        return None if runtime is None else runtime.status()

    def consent_update_download(self, *, version: str, confirm: bool) -> dict[str, Any]:
        return self._update_action(
            lambda runtime: runtime.consent_download(version=version, confirm=confirm)
        )

    def download_update(self) -> dict[str, Any]:
        return self._update_action(lambda runtime: runtime.start_download())

    def consent_update_install(self, *, version: str, confirm: bool) -> dict[str, Any]:
        return self._update_action(
            lambda runtime: runtime.consent_install(version=version, confirm=confirm)
        )

    def install_update(self) -> dict[str, Any]:
        return self._update_action(lambda runtime: runtime.start_install())

    def resume_update(self) -> dict[str, Any]:
        return self._update_action(lambda runtime: runtime.resume())

    def _update_action(
        self, action: Callable[[UpdateRuntimeService], dict[str, Any]]
    ) -> dict[str, Any]:
        runtime = self.update_runtime
        if runtime is None:
            raise RuntimeRequestError(
                503,
                "capability_unavailable",
                "Update installation is not configured in this build.",
                {"capability": "update_runtime"},
            )
        try:
            return action(runtime)
        except UpdateRuntimeError as error:
            raise RuntimeRequestError(error.status, error.code, error.message) from None

    @property
    def active_profile_id(self) -> str | None:
        with self._lock:
            return self._config_result.config.profiles.active_profile_id

    def list_profiles(self) -> dict[str, Any]:
        service = self._require_profiles()
        active = self.active_profile_id
        return {
            "active_profile_id": active,
            "profiles": [self._public_profile(profile, active=active) for profile in service.list_all()],
        }

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self._get_profile(profile_id)
        return self._public_profile(profile, active=self.active_profile_id)

    def create_profile(self, *, display_name: str, admin_key: str) -> dict[str, Any]:
        service = self._require_profiles()
        store = self._require_credentials()
        identifier = new_profile_id()
        verification = self._verify_credential(admin_key)
        self._assert_unique_credential(admin_key)
        reference = None
        try:
            reference = store.put(identifier.value, admin_key)
            profile = service.create(
                display_name,
                reference,
                organization_ref=verification.authoritative_opaque_identity,
            )
            profile = service.mark_validated(profile.profile_id)
        except DuplicateProfileError:
            if reference is not None:
                try:
                    store.delete(reference)
                except Exception:
                    pass
            raise RuntimeRequestError(409, "profile_conflict", "The profile already exists.") from None
        except (TypeError, ValueError) as error:
            if reference is not None:
                try:
                    store.delete(reference)
                except Exception:
                    pass
            raise RuntimeRequestError(
                400, "invalid_profile", "The profile is invalid.", {"detail": str(error)}
            ) from None
        except AdapterUnavailableError:
            raise RuntimeRequestError(
                503, "capability_unavailable", "Secure credential storage is unavailable.",
                {"capability": "credential_store"},
            ) from None
        except Exception:
            if reference is not None:
                try:
                    store.delete(reference)
                except Exception:
                    pass
            raise
        return self._public_profile(profile, active=self.active_profile_id)

    def update_profile(
        self,
        profile_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        service = self._require_profiles()
        identifier = self._profile_id(profile_id)
        if display_name is None and enabled is None:
            raise RuntimeRequestError(400, "empty_update", "At least one profile field is required.")
        if enabled is not None and not isinstance(enabled, bool):
            raise RuntimeRequestError(
                400,
                "invalid_profile",
                "The profile is invalid.",
                {"detail": "enabled must be a boolean"},
            )
        try:
            profile = service.get(identifier)
            if display_name is not None:
                validated_name = profile.renamed(display_name).display_name
                profile = service.rename(identifier, validated_name)
            if enabled is not None:
                profile = service.set_enabled(identifier, enabled)
        except ProfileNotFoundError:
            raise self._profile_not_found() from None
        except (TypeError, ValueError) as error:
            raise RuntimeRequestError(
                400, "invalid_profile", "The profile is invalid.", {"detail": str(error)}
            ) from None
        return self._public_profile(profile, active=self.active_profile_id)

    def delete_profile(self, profile_id: str) -> None:
        service = self._require_profiles()
        identifier = self._profile_id(profile_id)
        if self.active_profile_id == identifier.value:
            raise RuntimeRequestError(
                409,
                "active_profile_conflict",
                "Activate another profile before deleting this profile.",
            )
        try:
            removed = service.delete(identifier)
        except ProfileNotFoundError:
            raise self._profile_not_found() from None
        except ProfileHasDataError:
            raise RuntimeRequestError(
                409,
                "profile_has_history",
                "Profile history must be removed through explicit retention operations first.",
            ) from None
        store = self.credential_store
        if store is not None and store.available:
            try:
                store.delete(removed.credential_ref)
            except Exception:
                pass

    def activate_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self._get_profile(profile_id)
        if not profile.enabled:
            raise RuntimeRequestError(409, "profile_disabled", "The profile is disabled.")
        with self._lock:
            current = self._config_result.config
            updated = replace(
                current,
                profiles=replace(current.profiles, active_profile_id=profile.profile_id.value),
            )
            try:
                self.config_service.save(updated)
            except Exception:
                raise RuntimeRequestError(
                    500,
                    "config_write_failed",
                    "The active profile could not be saved.",
                ) from None
            self._config_result = ConfigLoadResult(
                config=updated,
                source=ConfigLoadSource.FILE,
                config_path=self.paths.config_file,
            )
            self._profile_generation += 1
        return self._public_profile(profile, active=profile.profile_id.value)

    def replace_profile_credential(self, profile_id: str, *, admin_key: str) -> dict[str, Any]:
        service = self._require_profiles()
        store = self._require_credentials()
        identifier = self._profile_id(profile_id)
        self._get_profile(profile_id)
        verification = self._verify_credential(admin_key)
        self._assert_unique_credential(admin_key, excluding=identifier)
        try:
            reference = store.put(identifier.value, admin_key)
            profile = service.replace_credential(identifier, reference)
            # Profile identity can only come from the verifier. A replacement
            # credential clears an older identity when the new authoritative
            # check provides capability only.
            profile = service.set_organization_ref(
                identifier,
                verification.authoritative_opaque_identity,
            )
            profile = service.mark_validated(identifier)
        except (AdapterUnavailableError, CredentialNotFoundError):
            raise RuntimeRequestError(
                503, "capability_unavailable", "Secure credential storage is unavailable.",
                {"capability": "credential_store"},
            ) from None
        return self._public_profile(profile, active=self.active_profile_id)

    def delete_profile_credential(self, profile_id: str) -> dict[str, Any]:
        service = self._require_profiles()
        store = self._require_credentials()
        identifier = self._profile_id(profile_id)
        try:
            reference = service.credential_for(identifier)
            store.delete(reference)
            profile = service.set_enabled(identifier, False)
        except ProfileNotFoundError:
            raise self._profile_not_found() from None
        except (AdapterUnavailableError, CredentialNotFoundError):
            raise RuntimeRequestError(
                503, "capability_unavailable", "Secure credential storage is unavailable.",
                {"capability": "credential_store"},
            ) from None
        return self._public_profile(profile, active=self.active_profile_id)

    def sync_usage(
        self,
        *,
        profile_id: str | None = None,
        start_utc: int | None = None,
        end_utc: int | None = None,
        days: int = 30,
        resume: bool = True,
    ) -> dict[str, Any]:
        database = self._require_database()
        if not isinstance(resume, bool):
            raise RuntimeRequestError(
                400, "invalid_sync_request", "The sync request is invalid.",
                {"detail": "resume must be a boolean"},
            )
        if profile_id is not None:
            profile_id = self._profile_id(profile_id).value
        profile, generation = self._active_profile_snapshot(profile_id)
        client = self._client_for_profile(profile.profile_id)
        if self.project_keys is None:
            raise RuntimeRequestError(
                503,
                "capability_unavailable",
                "Project pseudonymization is unavailable.",
                {"capability": "project_keys"},
            )
        catalog_version = self.catalog.get("catalog_version")
        if not isinstance(catalog_version, str) or not catalog_version:
            raise RuntimeRequestError(
                503,
                "capability_unavailable",
                "The model catalog is unavailable.",
                {"capability": "catalog"},
            )
        try:
            with self._data_lock:
                result = UsageSyncService(
                    database,
                    project_keys=self.project_keys,
                    catalog_version=catalog_version,
                    profile_id=profile.profile_id.value,
                ).sync(
                    client,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    days=days,
                    resume=resume,
                )
        except ValueError as error:
            raise RuntimeRequestError(
                400, "invalid_sync_request", "The sync request is invalid.",
                {"detail": str(error)},
            ) from None
        self._assert_profile_snapshot(profile.profile_id.value, generation)
        return asdict(result)

    def history(
        self,
        *,
        start_utc: int,
        end_utc: int,
        profile_id: str | None = None,
        project_key: str | None = None,
        include_missing: bool = True,
    ) -> dict[str, Any]:
        database = self._require_database()
        profile = self._resolve_profile(profile_id)
        self._validate_project_key(project_key)
        try:
            with self._data_lock:
                records = database.daily_usage(
                    start_utc,
                    end_utc,
                    project_key=project_key,
                    catalog=self.catalog,
                    include_missing=include_missing,
                    profile_id=profile.profile_id.value,
                )
        except ValueError as error:
            raise RuntimeRequestError(
                400, "invalid_history_query", "The history query is invalid.",
                {"detail": str(error)},
            ) from None
        counts = {"complete": 0, "partial": 0, "missing": 0}
        for item in records:
            counts[item["completeness"]] += 1
        return {
            "profile_id": profile.profile_id.value,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "project_key": project_key,
            "completeness": counts,
            "records": records,
        }

    def preview_retention(
        self, *, retention_days: int, profile_id: str | None = None
    ) -> dict[str, Any]:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= 3650
        ):
            raise RuntimeRequestError(
                400,
                "invalid_retention_days",
                "Retention days must be between 1 and 3650.",
            )
        database = self._require_database()
        profile = self._resolve_profile(profile_id)
        try:
            with self._data_lock:
                preview = HistoryOperations(
                    database, profile_id=profile.profile_id.value
                ).preview_retention(retention_days)
        except ValueError:
            raise RuntimeRequestError(
                400, "retention_preview_failed", "The retention preview could not be created."
            ) from None
        token = secrets.token_urlsafe(24)
        with self._lock:
            while token in self._retention_previews:
                token = secrets.token_urlsafe(24)
            if len(self._retention_previews) >= 32:
                self._retention_previews.pop(next(iter(self._retention_previews)))
            self._retention_previews[token] = preview
        return {
            "preview_token": token,
            "profile_id": preview.profile_id,
            "retention_days": preview.retention_days,
            "cutoff_utc": preview.cutoff_utc,
            "cutoff": epoch_to_utc_text(preview.cutoff_utc),
            "row_count": preview.row_count,
            "oldest_bucket_start_utc": preview.oldest_bucket_start_utc,
            "newest_bucket_end_utc": preview.newest_bucket_end_utc,
        }

    def apply_retention(self, *, preview_token: str, confirm: bool) -> dict[str, Any]:
        if confirm is not True:
            raise RuntimeRequestError(
                400,
                "retention_confirmation_required",
                "Retention deletion requires explicit confirmation.",
            )
        if not isinstance(preview_token, str) or not _RETENTION_TOKEN.fullmatch(preview_token):
            raise RuntimeRequestError(
                400, "invalid_retention_preview", "The retention preview token is invalid."
            )
        with self._lock:
            preview = self._retention_previews.pop(preview_token, None)
        if preview is None:
            raise RuntimeRequestError(
                409,
                "retention_preview_expired",
                "The retention preview is unavailable; create a new preview.",
            )
        database = self._require_database()
        try:
            with self._data_lock:
                result = HistoryOperations(
                    database, profile_id=preview.profile_id
                ).apply_retention(preview, confirm=True)
        except ValueError as error:
            code = (
                "retention_preview_stale"
                if "changed after preview" in str(error)
                else "retention_apply_failed"
            )
            raise RuntimeRequestError(
                409,
                code,
                "History changed after the preview; create a new preview."
                if code == "retention_preview_stale"
                else "Retention could not be applied safely.",
            ) from None
        return {
            "profile_id": preview.profile_id,
            "cutoff_utc": result.cutoff_utc,
            "cutoff": epoch_to_utc_text(result.cutoff_utc),
            "deleted_rows": result.deleted_rows,
        }

    def projects(self, *, profile_id: str | None = None) -> dict[str, Any]:
        database = self._require_database()
        profile = self._resolve_profile(profile_id)
        with self._data_lock:
            records = database.list_projects(profile_id=profile.profile_id.value)
        safe_records = []
        for item in records:
            display_name = item["display_name"]
            raw_identifier = item.get("project_id_private")
            if raw_identifier and raw_identifier in display_name:
                display_name = f"Project {item['project_key'][-8:]}"
            safe_records.append(
                {
                    "project_key": item["project_key"],
                    "display_name": display_name,
                    "bucket_count": item["bucket_count"],
                }
            )
        return {
            "profile_id": profile.profile_id.value,
            "projects": safe_records,
        }

    def export_bytes(
        self,
        *,
        format: str,
        start_utc: int,
        end_utc: int,
        profile_id: str | None = None,
        project_key: str | None = None,
        project_id_policy: str = "mask",
    ) -> tuple[bytes, str, str]:
        database = self._require_database()
        profile = self._resolve_profile(profile_id)
        self._validate_project_key(project_key)
        if not isinstance(format, str) or format not in {"csv", "json"}:
            raise RuntimeRequestError(400, "invalid_export_format", "Export format must be csv or json.")
        if not isinstance(project_id_policy, str) or project_id_policy not in {"mask", "exclude"}:
            raise RuntimeRequestError(
                400,
                "unsafe_export_policy",
                "The HTTP and operations interfaces only allow masked or excluded project IDs.",
            )
        try:
            with self._data_lock:
                records = build_export_records(
                    database,
                    start_utc,
                    end_utc,
                    project_key=project_key,
                    project_id_policy=project_id_policy,
                    profile_id=profile.profile_id.value,
                )
            if format == "csv":
                body = render_csv(records)
                media_type = "text/csv; charset=utf-8"
            else:
                body = render_json(
                    records,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    project_key=project_key,
                    project_id_policy=project_id_policy,
                    profile_id=profile.profile_id.value,
                )
                media_type = "application/json; charset=utf-8"
        except ValueError as error:
            raise RuntimeRequestError(
                400, "invalid_export_request", "The export request is invalid.",
                {"detail": str(error)},
            ) from None
        filename = f"usage-{profile.profile_id.value}-{start_utc}-{end_utc}.{format}"
        return body, media_type, filename

    def list_alerts(self, *, profile_id: str | None = None) -> dict[str, Any]:
        state = self._require_alerts()
        profile = self._resolve_profile(profile_id)
        with self._data_lock:
            rules = state.list_rules(profile.profile_id.value)
        return {
            "profile_id": profile.profile_id.value,
            "rules": [self._public_alert_rule(rule) for rule in rules],
        }

    def save_alert(
        self,
        *,
        profile_id: str | None,
        group_id: str,
        threshold_percent: float,
        project_key: str = "all",
        enabled: bool = True,
        rule_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._require_alerts()
        profile = self._resolve_profile(profile_id)
        identifier = rule_id or f"rule_{uuid.uuid4().hex}"
        if not isinstance(identifier, str) or not _ALERT_TOKEN.fullmatch(identifier):
            raise RuntimeRequestError(400, "invalid_alert", "The alert rule identifier is invalid.")
        if not isinstance(group_id, str) or not _ALERT_TOKEN.fullmatch(group_id):
            raise RuntimeRequestError(400, "invalid_alert", "The alert group is invalid.")
        self._validate_project_key(project_key, allow_all=True)
        if not isinstance(enabled, bool):
            raise RuntimeRequestError(400, "invalid_alert", "Alert enabled must be a boolean.")
        if isinstance(threshold_percent, bool) or not isinstance(threshold_percent, (int, float)):
            raise RuntimeRequestError(400, "invalid_alert", "Alert threshold must be a number.")
        try:
            rule = AlertRule(
                identifier,
                profile.profile_id.value,
                group_id,
                threshold_percent,
                project_key,
                enabled,
            )
            with self._data_lock:
                state.save_rule(rule)
        except (TypeError, ValueError) as error:
            raise RuntimeRequestError(
                400, "invalid_alert", "The alert rule is invalid.", {"detail": str(error)}
            ) from None
        return self._public_alert_rule(rule)

    def delete_alert(self, rule_id: str, *, profile_id: str | None = None) -> None:
        state = self._require_alerts()
        profile = self._resolve_profile(profile_id)
        if not _ALERT_TOKEN.fullmatch(rule_id):
            raise RuntimeRequestError(400, "invalid_alert", "The alert rule identifier is invalid.")
        with self._data_lock:
            deleted = state.delete_rule(profile.profile_id.value, rule_id)
        if not deleted:
            raise RuntimeRequestError(404, "alert_not_found", "The alert rule was not found.")

    def alert_history(
        self, *, profile_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        state = self._require_alerts()
        profile = self._resolve_profile(profile_id)
        try:
            with self._data_lock:
                records = state.notification_history(profile.profile_id.value, limit=limit)
        except ValueError as error:
            raise RuntimeRequestError(
                400, "invalid_alert_history_query", "The alert history query is invalid.",
                {"detail": str(error)},
            ) from None
        return {"profile_id": profile.profile_id.value, "records": list(records)}

    def send_test_notification(self, *, profile_id: str | None = None) -> dict[str, Any]:
        """Send and record a generic notification without exposing profile labels."""

        state = self._require_alerts()
        profile = self._resolve_profile(profile_id)
        adapter = self.notification_adapter
        if adapter is None or not adapter.available:
            raise RuntimeRequestError(
                503,
                "capability_unavailable",
                "Desktop notifications are unavailable.",
                {"capability": "notifications"},
            )
        occurred_at = datetime.now(timezone.utc)
        event = AlertEvent(
            "notification_test",
            profile.profile_id.value,
            "system",
            "all",
            100,
            100,
            occurred_at,
            test=True,
        )
        message = NotificationMessage(
            "notification.test_title",
            "notification.test_body",
            deep_link="/settings?section=notifications",
        )
        try:
            delivered = adapter.send(message)
        except Exception:
            delivered = False
        with self._data_lock:
            notification_id = state.record_notification(
                event,
                delivery_status="test" if delivered else "failed",
                error_code=None if delivered else "notification_unavailable",
            )
        if not delivered:
            raise RuntimeRequestError(
                503,
                "capability_unavailable",
                "Desktop notifications are unavailable or permission was denied.",
                {"capability": "notifications"},
            )
        return {
            "sent": True,
            "profile_id": profile.profile_id.value,
            "notification_id": notification_id,
        }

    def integrity(self, *, full: bool = False) -> dict[str, Any]:
        database = self._require_database()
        with self._data_lock:
            result = database.check_integrity(full=full)
        return {
            "ok": result.ok,
            "messages": list(result.messages),
            "read_only": result.read_only,
            "guidance": result.guidance,
        }

    def create_managed_backup(self) -> dict[str, Any]:
        database = self._require_database()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"history-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        destination = self.paths.data_dir / "backups" / name
        try:
            with self._data_lock:
                database.backup_to(destination)
        except Exception:
            raise RuntimeRequestError(500, "backup_failed", "The history backup could not be created.") from None
        return {"backup_name": name, "created_at": epoch_to_utc_text(int(datetime.now(timezone.utc).timestamp()))}

    def restore_managed_backup(self, backup_name: str, *, confirm: bool = False) -> dict[str, Any]:
        database = self._require_database()
        if confirm is not True:
            raise RuntimeRequestError(
                409, "confirmation_required", "Restore requires explicit confirmation."
            )
        if not isinstance(backup_name, str) or not _MANAGED_BACKUP.fullmatch(backup_name):
            raise RuntimeRequestError(400, "invalid_backup_name", "The backup name is invalid.")
        source = self.paths.data_dir / "backups" / backup_name
        try:
            with self._data_lock:
                database.replace_from_backup(source)
        except FileNotFoundError:
            raise RuntimeRequestError(404, "backup_not_found", "The backup was not found.") from None
        except Exception:
            raise RuntimeRequestError(500, "restore_failed", "The history backup could not be restored.") from None
        return {"restored": True, "backup_name": backup_name}

    def _require_database(self) -> DatabaseService:
        if self.database is None:
            raise RuntimeRequestError(
                503, "capability_unavailable", "History storage is unavailable.",
                {"capability": "history"},
            )
        return self.database

    def _require_profiles(self) -> ProfileService:
        if self.profile_service is None:
            raise RuntimeRequestError(
                503, "capability_unavailable", "Profiles are unavailable.",
                {"capability": "profiles"},
            )
        return self.profile_service

    def _require_credentials(self) -> CredentialStore:
        store = self.credential_store
        if store is None or not store.available:
            raise RuntimeRequestError(
                503, "capability_unavailable", "Secure credential storage is unavailable.",
                {"capability": "credential_store"},
            )
        return store

    def _require_alerts(self) -> SQLiteAlertState:
        if self.alert_state is None:
            raise RuntimeRequestError(
                503, "capability_unavailable", "Alert persistence is unavailable.",
                {"capability": "alerts"},
            )
        return self.alert_state

    @staticmethod
    def _profile_id(profile_id: str) -> ProfileId:
        try:
            return ProfileId(profile_id)
        except (TypeError, ValueError):
            raise RuntimeRequestError(400, "invalid_profile_id", "The profile identifier is invalid.") from None

    @staticmethod
    def _profile_not_found() -> RuntimeRequestError:
        return RuntimeRequestError(404, "profile_not_found", "The profile was not found.")

    def _get_profile(self, profile_id: str):
        try:
            return self._require_profiles().get(self._profile_id(profile_id))
        except ProfileNotFoundError:
            raise self._profile_not_found() from None

    def _resolve_profile(self, profile_id: str | None):
        selected = profile_id or self.active_profile_id
        if selected is None:
            raise RuntimeRequestError(409, "no_active_profile", "No profile is active.")
        return self._get_profile(selected)

    def _active_profile_snapshot(self, profile_id: str | None):
        with self._lock:
            active = self._config_result.config.profiles.active_profile_id
            generation = self._profile_generation
        if active is None:
            raise RuntimeRequestError(409, "no_active_profile", "No profile is active.")
        if profile_id is not None and profile_id != active:
            raise RuntimeRequestError(
                409, "active_profile_mismatch", "Sync is restricted to the active profile."
            )
        profile = self._get_profile(active)
        if not profile.enabled:
            raise RuntimeRequestError(409, "profile_disabled", "The profile is disabled.")
        return profile, generation

    def _assert_profile_snapshot(self, profile_id: str, generation: int) -> None:
        with self._lock:
            active = self._config_result.config.profiles.active_profile_id
            current_generation = self._profile_generation
        if active != profile_id or current_generation != generation:
            raise RuntimeRequestError(
                409,
                "active_profile_changed",
                "The active profile changed while the request was running; discard this result.",
                {"profile_id": profile_id},
            )

    def _verify_credential(self, admin_key: str) -> CredentialVerification:
        if not isinstance(admin_key, str) or len(admin_key) > 512:
            raise RuntimeRequestError(400, "invalid_admin_key", "The Admin API Key is invalid.")
        if self.credential_verifier is None:
            raise RuntimeRequestError(
                503, "capability_unavailable", "Credential verification is unavailable.",
                {"capability": "credential_verification"},
            )
        client = self._new_client(admin_key)
        try:
            verification = self.credential_verifier.verify(client)
        except OpenAIClientError as error:
            raise RuntimeRequestError(error.http_status, error.code, error.message) from None
        except Exception:
            raise RuntimeRequestError(
                502, "credential_verification_failed", "The Admin API Key could not be verified."
            ) from None
        if not isinstance(verification, CredentialVerification) or not verification.capability_verified:
            raise RuntimeRequestError(
                403, "usage_capability_missing", "The key cannot read organization usage."
            )
        identity = verification.authoritative_opaque_identity
        if identity is not None and (
            not isinstance(identity, str)
            or not identity.strip()
            or len(identity) > 200
            or identity.startswith(("sk-admin-", "sk-proj-"))
        ):
            raise RuntimeRequestError(
                502,
                "credential_verification_failed",
                "The credential verifier returned an invalid identity.",
            )
        return verification

    def _assert_unique_credential(
        self, admin_key: str, *, excluding: ProfileId | None = None
    ) -> None:
        service = self._require_profiles()
        store = self._require_credentials()
        candidate = admin_key.encode("utf-8")
        duplicate = False
        for profile in service.list_all():
            if excluding is not None and profile.profile_id == excluding:
                continue
            try:
                existing = store.get(profile.credential_ref)
            except (AdapterUnavailableError, CredentialNotFoundError):
                continue
            except Exception:
                continue
            try:
                duplicate = hmac.compare_digest(candidate, existing.encode("utf-8")) or duplicate
            finally:
                existing = ""
        if duplicate:
            raise RuntimeRequestError(
                409,
                "duplicate_credential",
                "This credential is already assigned to another profile.",
            )

    def _new_client(self, admin_key: str) -> AdminUsageClient:
        factory = self.admin_client_factory
        if factory is None:
            raise RuntimeRequestError(
                503, "capability_unavailable", "The upstream client is unavailable.",
                {"capability": "upstream_client"},
            )
        try:
            return factory(admin_key, self.config.network.request_timeout_seconds)
        except OpenAIClientError as error:
            raise RuntimeRequestError(error.http_status, error.code, error.message) from None
        except (TypeError, ValueError):
            raise RuntimeRequestError(400, "invalid_admin_key", "The Admin API Key is invalid.") from None

    def _client_for_profile(self, profile_id: ProfileId) -> AdminUsageClient:
        service = self._require_profiles()
        store = self._require_credentials()
        try:
            reference = service.credential_for(profile_id)
            secret = store.get(reference)
        except ProfileNotFoundError:
            raise self._profile_not_found() from None
        except (AdapterUnavailableError, CredentialNotFoundError):
            raise RuntimeRequestError(
                409, "credential_missing", "The profile does not have an available credential."
            ) from None
        return self._new_client(secret)

    def _public_profile(self, profile, *, active: str | None) -> dict[str, Any]:
        configured = False
        store = self.credential_store
        if store is not None and store.available:
            try:
                store.get(profile.credential_ref)
                configured = True
            except Exception:
                configured = False
        if profile.organization_ref is not None:
            identity_status = "authoritative_opaque_identity"
        elif profile.last_validated_at is not None:
            identity_status = "capability_verified"
        else:
            identity_status = "unverified"
        return {
            "profile_id": profile.profile_id.value,
            "display_name": profile.display_name,
            "enabled": profile.enabled,
            "active": profile.profile_id.value == active,
            "credential_configured": configured,
            "identity_status": identity_status,
            "created_at": profile.created_at.isoformat().replace("+00:00", "Z"),
            "last_validated_at": (
                profile.last_validated_at.isoformat().replace("+00:00", "Z")
                if profile.last_validated_at is not None
                else None
            ),
        }

    @staticmethod
    def _public_alert_rule(rule: AlertRule) -> dict[str, Any]:
        return {
            "rule_id": rule.rule_id,
            "profile_id": rule.profile_id,
            "group_id": rule.group_id,
            "threshold_percent": rule.threshold_percent,
            "project_key": rule.project_key,
            "enabled": rule.enabled,
        }

    @staticmethod
    def _validate_project_key(value: str | None, *, allow_all: bool = False) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not _PROJECT_KEY.fullmatch(value) or (
            value == "all" and not allow_all
        ):
            raise RuntimeRequestError(400, "invalid_project_key", "The project filter is invalid.")

    def _apply_startup(self, enabled: bool) -> None:
        adapter = self.startup_adapter
        if adapter is None or not adapter.available:
            raise RuntimeCapabilityUnavailable("startup")
        try:
            applied = adapter.enable() if enabled else adapter.disable()
        except Exception as error:
            raise RuntimeApplyError("startup setting could not be applied") from error
        if applied is not True:
            raise RuntimeApplyError("startup setting could not be applied")

    def _rollback_startup(self, enabled: bool) -> None:
        adapter = self.startup_adapter
        if adapter is None or not adapter.available:
            return
        try:
            if enabled:
                adapter.enable()
            else:
                adapter.disable()
        except Exception:
            return


def _nested_value(document: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = document
    for part in dotted_field.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value
