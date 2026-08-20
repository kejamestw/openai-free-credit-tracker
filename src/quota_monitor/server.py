import json
import mimetypes
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from .config_service import ConfigError, ConfigService, ConfigWriteError
from .cost_service import fetch_costs
from .model_catalog import load_catalog, resource_path
from .openai_client import OpenAIAdminClient, OpenAIClientError, validate_admin_key
from .platform_adapters.deep_links import DeepLinkValidationError, parse_deep_link
from .runtime import (
    RuntimeApplyError,
    RuntimeCapabilityUnavailable,
    RuntimeRequestError,
    RuntimeServices,
    OpenAIUsageCredentialVerifier,
    UpdateCheckService,
)
from .update_manifest import UpdateCheckResult, UpdateStatus
from .usage_service import UsageDataError, fetch_usage
from .version import __version__


class PublicHTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, params: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.params = params or {}


MAX_JSON_BODY_BYTES = 64 * 1024
MAX_REQUEST_TARGET_BYTES = 4096
MAX_QUERY_FIELDS = 16
SUPPORTED_LOCALES = frozenset({"en", "zh-TW"})
SPA_ROUTES = frozenset({"/dashboard", "/profiles", "/settings", "/alerts"})


class Handler(BaseHTTPRequestHandler):
    web_root = resource_path("web")
    locale_root = resource_path("locales")
    catalog = load_catalog()
    client_factory = staticmethod(OpenAIAdminClient)
    runtime_services: RuntimeServices

    def log_message(self, *_):
        return

    def _request_id(self) -> str:
        request_id = getattr(self, "request_id", None)
        if request_id is None:
            request_id = uuid.uuid4().hex
            self.request_id = request_id
        return request_id

    def _log_result(self, status: int, event: str) -> None:
        print(
            f"request_id={self._request_id()} method={self.command} event={event} status={status}",
            flush=True,
        )

    def send_bytes(
        self,
        code: int,
        body: bytes,
        content_type: str,
        event: str,
        *,
        filename: str | None = None,
    ):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Request-ID", self._request_id())
        if filename is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)
        self._log_result(code, event)

    def send_json(self, code: int, payload: dict, event: str):
        self.send_bytes(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            event,
        )

    def send_public_error(self, error: PublicHTTPError):
        payload = {
            "code": error.code,
            "message": error.message,
            "request_id": self._request_id(),
        }
        if error.params:
            payload["params"] = error.params
        self.send_json(
            error.status,
            {"error": payload},
            error.code,
        )

    def _validate_host(self) -> None:
        host = self.headers.get("Host", "")
        parts = host.rsplit(":", 1)
        hostname = parts[0].lower()
        if hostname not in {"127.0.0.1", "localhost"}:
            raise PublicHTTPError(403, "invalid_host", "This local server only accepts loopback requests.")
        if len(parts) == 2:
            try:
                port = int(parts[1])
            except ValueError:
                raise PublicHTTPError(403, "invalid_host", "This local server only accepts loopback requests.") from None
            if port != self.server.server_port:
                raise PublicHTTPError(403, "invalid_host", "This local server only accepts its active port.")

    def _validate_browser_boundary(self) -> None:
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site not in {"", "none", "same-origin"}:
            raise PublicHTTPError(403, "cross_site_request", "Cross-site requests are not accepted.")
        origin = self.headers.get("Origin")
        if origin is None:
            return
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise PublicHTTPError(403, "invalid_origin", "This local API only accepts its own browser origin.")
        if parsed.port != self.server.server_port:
            raise PublicHTTPError(403, "invalid_origin", "This local API only accepts its active port.")

    def _static_path(self, raw_path: str) -> Path:
        try:
            decoded = unquote(raw_path, errors="strict").replace("\\", "/")
        except UnicodeError:
            raise PublicHTTPError(400, "invalid_path", "The requested path is invalid.") from None
        segments = decoded.split("/")
        if "\x00" in decoded or any(segment == ".." for segment in segments):
            raise PublicHTTPError(403, "forbidden_path", "The requested resource is not available.")
        filename = "index.html" if decoded == "/" or decoded in SPA_ROUTES else decoded.lstrip("/")
        root = self.web_root.resolve()
        file_path = (root / filename).resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            raise PublicHTTPError(403, "forbidden_path", "The requested resource is not available.") from None
        return file_path

    def _request_target(self):
        if len(self.path.encode("utf-8", errors="surrogatepass")) > MAX_REQUEST_TARGET_BYTES:
            raise PublicHTTPError(
                414,
                "request_target_too_large",
                "The request target is too large.",
                {"maximum_bytes": MAX_REQUEST_TARGET_BYTES},
            )
        return urlsplit(self.path)

    @staticmethod
    def _runtime_error(error: RuntimeRequestError) -> PublicHTTPError:
        return PublicHTTPError(error.status, error.code, error.message, error.params)

    def _query(self, target, *, allowed: set[str]) -> dict[str, str]:
        try:
            pairs = parse_qsl(
                target.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=MAX_QUERY_FIELDS,
            )
        except ValueError:
            raise PublicHTTPError(400, "invalid_query", "The query string is invalid.") from None
        values: dict[str, str] = {}
        for key, value in pairs:
            if key not in allowed:
                raise PublicHTTPError(
                    400, "unknown_query_field", "The query contains an unknown field.",
                )
            if key in values:
                raise PublicHTTPError(
                    400, "duplicate_query_field", "A query field was provided more than once.",
                    {"field": key},
                )
            values[key] = value
        return values

    @staticmethod
    def _integer_query(
        values: dict[str, str], key: str, *, required: bool = False, default: int | None = None
    ) -> int | None:
        value = values.get(key)
        if value is None:
            if required:
                raise PublicHTTPError(
                    400, "missing_query_field", "A required query field is missing.",
                    {"field": key},
                )
            return default
        if not value.isascii() or not value.isdigit() or len(value) > 20:
            raise PublicHTTPError(
                400, "invalid_query_field", "A query field has an invalid value.",
                {"field": key},
            )
        return int(value)

    @staticmethod
    def _boolean_query(
        values: dict[str, str], key: str, *, default: bool = False
    ) -> bool:
        value = values.get(key)
        if value is None:
            return default
        if value not in {"true", "false"}:
            raise PublicHTTPError(
                400, "invalid_query_field", "A query field has an invalid value.",
                {"field": key},
            )
        return value == "true"

    @staticmethod
    def _body_fields(document: dict, *, allowed: set[str], required: set[str] = frozenset()) -> None:
        unknown = sorted(document.keys() - allowed)
        if unknown:
            raise PublicHTTPError(
                400, "unknown_request_field", "The JSON body contains an unknown field.",
            )
        missing = sorted(required - document.keys())
        if missing:
            raise PublicHTTPError(
                400, "missing_request_field", "The JSON body is missing a required field.",
                {"field": missing[0]},
            )

    def do_GET(self):
        self.request_id = uuid.uuid4().hex
        try:
            self._validate_host()
            target = self._request_target()
            path = target.path
            if path.startswith("/api/"):
                self._validate_browser_boundary()
            if path == "/api/v1/health":
                return self.send_json(
                    200,
                    {"version": __version__, "api_version": "v1", "ready": True},
                    "health",
                )
            if path == "/api/v1/config":
                payload = self.runtime_services.config_response()
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "config_read")
            if path.startswith("/api/v1/locales/"):
                return self._send_locale(path.removeprefix("/api/v1/locales/"))
            if path == "/api/v1/update/status":
                self._query(target, allowed=set())
                payload = self.runtime_services.update_status()
                if payload is None:
                    raise PublicHTTPError(
                        503,
                        "capability_unavailable",
                        "Update installation is not configured in this build.",
                        {"capability": "update_runtime"},
                    )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "update_status")
            if path == "/api/v1/update/check":
                return self._send_update_check(prepare=False)
            if path in {"/api/catalog", "/api/v1/catalog"}:
                return self.send_json(200, {**self.catalog, "version": __version__}, "catalog")
            if path in {"/api/data", "/api/v1/data"}:
                return self._send_data()
            if path == "/api/v1/profiles":
                self._query(target, allowed=set())
                payload = self.runtime_services.list_profiles()
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "profiles_list")
            if path.startswith("/api/v1/profiles/"):
                self._query(target, allowed=set())
                profile_id = path.removeprefix("/api/v1/profiles/")
                if not profile_id or "/" in profile_id:
                    raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                payload = self.runtime_services.get_profile(profile_id)
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "profile_read")
            if path == "/api/v1/history":
                query = self._query(
                    target,
                    allowed={"start_utc", "end_utc", "profile_id", "project_key", "include_missing"},
                )
                payload = self.runtime_services.history(
                    start_utc=self._integer_query(query, "start_utc", required=True),
                    end_utc=self._integer_query(query, "end_utc", required=True),
                    profile_id=query.get("profile_id") or None,
                    project_key=query.get("project_key") or None,
                    include_missing=self._boolean_query(query, "include_missing", default=True),
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "history")
            if path == "/api/v1/projects":
                query = self._query(target, allowed={"profile_id"})
                payload = self.runtime_services.projects(
                    profile_id=query.get("profile_id") or None
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "projects")
            if path == "/api/v1/alerts":
                query = self._query(target, allowed={"profile_id"})
                payload = self.runtime_services.list_alerts(
                    profile_id=query.get("profile_id") or None
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "alerts_list")
            if path == "/api/v1/alerts/history":
                query = self._query(target, allowed={"profile_id", "limit"})
                payload = self.runtime_services.alert_history(
                    profile_id=query.get("profile_id") or None,
                    limit=self._integer_query(query, "limit", default=100),
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "alert_history")
            if path == "/api/v1/operations/integrity":
                query = self._query(target, allowed={"full"})
                payload = self.runtime_services.integrity(
                    full=self._boolean_query(query, "full", default=False)
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "integrity")
            try:
                decoded_path = unquote(path, errors="strict").replace("\\", "/")
            except UnicodeError:
                raise PublicHTTPError(
                    400, "invalid_path", "The requested path is invalid."
                ) from None
            if decoded_path in SPA_ROUTES:
                try:
                    parse_deep_link(self.path)
                except DeepLinkValidationError:
                    raise PublicHTTPError(
                        400,
                        "invalid_navigation_target",
                        "The requested navigation target is invalid.",
                    ) from None
            file_path = self._static_path(path)
            if not file_path.is_file():
                raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            return self.send_bytes(200, file_path.read_bytes(), content_type, "static")
        except PublicHTTPError as exc:
            return self.send_public_error(exc)
        except RuntimeRequestError as exc:
            return self.send_public_error(self._runtime_error(exc))
        except Exception:
            return self.send_public_error(
                PublicHTTPError(500, "internal_error", "The local server could not complete the request.")
            )

    def do_PUT(self):
        self.request_id = uuid.uuid4().hex
        self._request_body_consumed = False
        try:
            self._validate_host()
            target = self._request_target()
            path = target.path
            if path.startswith("/api/"):
                self._validate_browser_boundary()
            self._query(target, allowed=set())
            if path != "/api/v1/config" and not path.startswith("/api/v1/profiles/"):
                raise PublicHTTPError(405, "method_not_allowed", "This method is not allowed.")
            document = self._read_json_object()
            if path.startswith("/api/v1/profiles/"):
                suffix = path.removeprefix("/api/v1/profiles/")
                if suffix.endswith("/credential"):
                    profile_id = suffix.removesuffix("/credential")
                    if not profile_id or "/" in profile_id:
                        raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                    self._body_fields(document, allowed={"admin_key"}, required={"admin_key"})
                    payload = self.runtime_services.replace_profile_credential(
                        profile_id, admin_key=document["admin_key"]
                    )
                    payload["request_id"] = self._request_id()
                    return self.send_json(200, payload, "profile_credential_saved")
                if not suffix or "/" in suffix:
                    raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                self._body_fields(document, allowed={"display_name", "enabled"})
                payload = self.runtime_services.update_profile(
                    suffix,
                    display_name=document.get("display_name"),
                    enabled=document.get("enabled"),
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "profile_saved")
            if path != "/api/v1/config":
                raise PublicHTTPError(405, "method_not_allowed", "This method is not allowed.")
            try:
                update = self.runtime_services.replace_config(document)
            except RuntimeCapabilityUnavailable as error:
                raise PublicHTTPError(
                    409,
                    "capability_unavailable",
                    "A required platform capability is unavailable.",
                    {"capability": error.capability},
                ) from None
            except RuntimeApplyError:
                raise PublicHTTPError(
                    500,
                    "config_apply_failed",
                    "The setting could not be applied. The previous configuration is still active.",
                ) from None
            except ConfigWriteError:
                raise PublicHTTPError(
                    500,
                    "config_write_failed",
                    "The configuration could not be saved. The previous configuration is still active.",
                ) from None
            except ConfigError as error:
                raise PublicHTTPError(
                    400,
                    "invalid_config",
                    "The configuration is invalid.",
                    {"detail": str(error)},
                ) from None
            payload = self.runtime_services.config_response()
            payload.update(
                {
                    "request_id": self._request_id(),
                    "restart_required": update.restart_required,
                    "restart_required_fields": list(update.restart_required_fields),
                    "applied_fields": list(update.applied_fields),
                }
            )
            return self.send_json(200, payload, "config_saved")
        except PublicHTTPError as exc:
            self._drain_unread_body()
            return self.send_public_error(exc)
        except RuntimeRequestError as exc:
            self._drain_unread_body()
            return self.send_public_error(self._runtime_error(exc))
        except Exception:
            self._drain_unread_body()
            return self.send_public_error(
                PublicHTTPError(500, "internal_error", "The local server could not complete the request.")
            )

    def do_POST(self):
        self.request_id = uuid.uuid4().hex
        self._request_body_consumed = False
        try:
            self._validate_host()
            target = self._request_target()
            path = target.path
            if path.startswith("/api/"):
                self._validate_browser_boundary()
            if path == "/api/v1/update/check":
                self._query(target, allowed=set())
                self._drain_unread_body()
                return self._send_update_check()
            update_actions = {
                "/api/v1/update/consent-download",
                "/api/v1/update/download",
                "/api/v1/update/consent-install",
                "/api/v1/update/install",
                "/api/v1/update/resume",
            }
            if path in update_actions:
                self._query(target, allowed=set())
                document = self._read_json_object()
                if path == "/api/v1/update/consent-download":
                    self._body_fields(
                        document,
                        allowed={"version", "confirm"},
                        required={"version", "confirm"},
                    )
                    payload = self.runtime_services.consent_update_download(
                        version=document["version"], confirm=document["confirm"]
                    )
                    event = "update_download_consented"
                    response_status = 200
                elif path == "/api/v1/update/download":
                    self._body_fields(document, allowed=set())
                    payload = self.runtime_services.download_update()
                    event = "update_download_started"
                    response_status = 202
                elif path == "/api/v1/update/consent-install":
                    self._body_fields(
                        document,
                        allowed={"version", "confirm"},
                        required={"version", "confirm"},
                    )
                    payload = self.runtime_services.consent_update_install(
                        version=document["version"], confirm=document["confirm"]
                    )
                    event = "update_install_consented"
                    response_status = 200
                elif path == "/api/v1/update/install":
                    self._body_fields(document, allowed=set())
                    payload = self.runtime_services.install_update()
                    event = "update_install_started"
                    response_status = 202
                else:
                    self._body_fields(document, allowed=set())
                    payload = self.runtime_services.resume_update()
                    event = "update_resume_started"
                    response_status = 202
                payload["request_id"] = self._request_id()
                return self.send_json(response_status, payload, event)
            if path in {
                "/api/v1/operations/retention/preview",
                "/api/v1/operations/retention/apply",
            }:
                self._query(target, allowed=set())
                document = self._read_json_object()
                if path.endswith("/preview"):
                    self._body_fields(
                        document,
                        allowed={"retention_days", "profile_id"},
                        required={"retention_days"},
                    )
                    payload = self.runtime_services.preview_retention(
                        retention_days=document["retention_days"],
                        profile_id=document.get("profile_id"),
                    )
                    event = "retention_preview"
                else:
                    self._body_fields(
                        document,
                        allowed={"preview_token", "confirm"},
                        required={"preview_token", "confirm"},
                    )
                    payload = self.runtime_services.apply_retention(
                        preview_token=document["preview_token"],
                        confirm=document["confirm"],
                    )
                    event = "retention_applied"
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, event)
            self._query(target, allowed=set())
            if path not in {
                "/api/v1/profiles",
                "/api/v1/sync",
                "/api/v1/export",
                "/api/v1/alerts",
                "/api/v1/notifications/test",
                "/api/v1/operations/backup",
                "/api/v1/operations/restore",
            } and not (
                path.startswith("/api/v1/profiles/") and path.endswith("/activate")
            ):
                raise PublicHTTPError(405, "method_not_allowed", "This method is not allowed.")
            document = self._read_json_object()
            if path == "/api/v1/profiles":
                self._body_fields(
                    document,
                    allowed={"display_name", "admin_key"},
                    required={"display_name", "admin_key"},
                )
                payload = self.runtime_services.create_profile(
                    display_name=document["display_name"], admin_key=document["admin_key"]
                )
                payload["request_id"] = self._request_id()
                return self.send_json(201, payload, "profile_created")
            if path.startswith("/api/v1/profiles/") and path.endswith("/activate"):
                profile_id = path.removeprefix("/api/v1/profiles/").removesuffix("/activate")
                if not profile_id or "/" in profile_id:
                    raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                self._body_fields(document, allowed=set())
                payload = self.runtime_services.activate_profile(profile_id)
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "profile_activated")
            if path == "/api/v1/sync":
                self._body_fields(
                    document,
                    allowed={"profile_id", "start_utc", "end_utc", "days", "resume"},
                )
                payload = self.runtime_services.sync_usage(
                    profile_id=document.get("profile_id"),
                    start_utc=document.get("start_utc"),
                    end_utc=document.get("end_utc"),
                    days=document.get("days", 30),
                    resume=document.get("resume", True),
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "sync")
            if path == "/api/v1/export":
                self._body_fields(
                    document,
                    allowed={
                        "format", "start_utc", "end_utc", "profile_id",
                        "project_key", "project_id_policy",
                    },
                    required={"format", "start_utc", "end_utc"},
                )
                body, media_type, filename = self.runtime_services.export_bytes(
                    format=document["format"],
                    start_utc=document["start_utc"],
                    end_utc=document["end_utc"],
                    profile_id=document.get("profile_id"),
                    project_key=document.get("project_key"),
                    project_id_policy=document.get("project_id_policy", "mask"),
                )
                return self.send_bytes(
                    200, body, media_type, "export", filename=filename
                )
            if path == "/api/v1/alerts":
                self._body_fields(
                    document,
                    allowed={
                        "profile_id", "rule_id", "group_id", "threshold_percent",
                        "project_key", "enabled",
                    },
                    required={"group_id", "threshold_percent"},
                )
                payload = self.runtime_services.save_alert(
                    profile_id=document.get("profile_id"),
                    rule_id=document.get("rule_id"),
                    group_id=document["group_id"],
                    threshold_percent=document["threshold_percent"],
                    project_key=document.get("project_key", "all"),
                    enabled=document.get("enabled", True),
                )
                payload["request_id"] = self._request_id()
                return self.send_json(201, payload, "alert_saved")
            if path == "/api/v1/notifications/test":
                self._body_fields(document, allowed={"profile_id"})
                payload = self.runtime_services.send_test_notification(
                    profile_id=document.get("profile_id")
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "notification_test_sent")
            if path == "/api/v1/operations/backup":
                self._body_fields(document, allowed=set())
                payload = self.runtime_services.create_managed_backup()
                payload["request_id"] = self._request_id()
                return self.send_json(201, payload, "backup_created")
            if path == "/api/v1/operations/restore":
                self._body_fields(
                    document,
                    allowed={"backup_name", "confirm"},
                    required={"backup_name", "confirm"},
                )
                payload = self.runtime_services.restore_managed_backup(
                    document["backup_name"], confirm=document["confirm"]
                )
                payload["request_id"] = self._request_id()
                return self.send_json(200, payload, "backup_restored")
            raise PublicHTTPError(405, "method_not_allowed", "This method is not allowed.")
        except PublicHTTPError as exc:
            self._drain_unread_body()
            return self.send_public_error(exc)
        except RuntimeRequestError as exc:
            self._drain_unread_body()
            return self.send_public_error(self._runtime_error(exc))
        except Exception:
            self._drain_unread_body()
            return self.send_public_error(
                PublicHTTPError(500, "internal_error", "The local server could not complete the request.")
            )

    def do_DELETE(self):
        self.request_id = uuid.uuid4().hex
        self._request_body_consumed = False
        try:
            self._validate_host()
            target = self._request_target()
            path = target.path
            if path.startswith("/api/"):
                self._validate_browser_boundary()
            if path.startswith("/api/v1/profiles/"):
                self._query(target, allowed=set())
                suffix = path.removeprefix("/api/v1/profiles/")
                if suffix.endswith("/credential"):
                    profile_id = suffix.removesuffix("/credential")
                    if not profile_id or "/" in profile_id:
                        raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                    self._drain_unread_body()
                    payload = self.runtime_services.delete_profile_credential(profile_id)
                    payload["request_id"] = self._request_id()
                    return self.send_json(200, payload, "profile_credential_deleted")
                if not suffix or "/" in suffix:
                    raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                self._drain_unread_body()
                self.runtime_services.delete_profile(suffix)
                return self.send_json(
                    200,
                    {"request_id": self._request_id(), "deleted": True},
                    "profile_deleted",
                )
            if path.startswith("/api/v1/alerts/"):
                query = self._query(target, allowed={"profile_id"})
                rule_id = path.removeprefix("/api/v1/alerts/")
                if not rule_id or "/" in rule_id:
                    raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
                self._drain_unread_body()
                self.runtime_services.delete_alert(
                    rule_id, profile_id=query.get("profile_id") or None
                )
                return self.send_json(
                    200,
                    {"request_id": self._request_id(), "deleted": True},
                    "alert_deleted",
                )
            raise PublicHTTPError(405, "method_not_allowed", "This method is not allowed.")
        except PublicHTTPError as exc:
            self._drain_unread_body()
            return self.send_public_error(exc)
        except RuntimeRequestError as exc:
            self._drain_unread_body()
            return self.send_public_error(self._runtime_error(exc))
        except Exception:
            self._drain_unread_body()
            return self.send_public_error(
                PublicHTTPError(500, "internal_error", "The local server could not complete the request.")
            )

    def _read_json_object(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise PublicHTTPError(
                400,
                "unsupported_transfer_encoding",
                "Transfer-Encoding is not supported by this local API.",
            )
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise PublicHTTPError(
                411,
                "content_length_required",
                "Content-Length is required.",
            )
        try:
            length = int(length_text)
        except ValueError:
            raise PublicHTTPError(
                400,
                "invalid_content_length",
                "Content-Length must be a non-negative integer.",
            ) from None
        if length < 0:
            raise PublicHTTPError(
                400,
                "invalid_content_length",
                "Content-Length must be a non-negative integer.",
            )
        if length > MAX_JSON_BODY_BYTES:
            # Drain only a bounded prefix. This lets ordinary clients receive the
            # 413 cleanly on Windows without accepting an unbounded request body.
            drain_size = min(length, MAX_JSON_BODY_BYTES + 1)
            self.rfile.read(drain_size)
            self._request_body_consumed = True
            self.close_connection = length > drain_size
            raise PublicHTTPError(
                413,
                "request_too_large",
                "The JSON request body is too large.",
                {"maximum_bytes": MAX_JSON_BODY_BYTES},
            )
        body = self.rfile.read(length)
        self._request_body_consumed = True
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise PublicHTTPError(
                415,
                "unsupported_media_type",
                "This endpoint accepts application/json.",
            )
        try:
            document = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except PublicHTTPError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublicHTTPError(
                400,
                "invalid_json",
                "The request body must be valid UTF-8 JSON.",
            ) from None
        if not isinstance(document, dict):
            raise PublicHTTPError(
                400,
                "invalid_json",
                "The request body must be a JSON object.",
            )
        return document

    def _drain_unread_body(self) -> None:
        if getattr(self, "_request_body_consumed", False):
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            self._request_body_consumed = True
            return
        drain_size = min(length, MAX_JSON_BODY_BYTES + 1)
        self.rfile.read(drain_size)
        self._request_body_consumed = length == drain_size
        self.close_connection = length > drain_size

    def _send_update_check(self, *, prepare: bool = True):
        try:
            result = self.runtime_services.check_for_updates(prepare=prepare)
        except Exception:
            raise PublicHTTPError(
                502,
                "update_check_failed",
                "The update service could not complete the check.",
            ) from None
        if result is None:
            raise PublicHTTPError(
                503,
                "capability_unavailable",
                "Update checking is not configured in this build.",
                {"capability": "update_check"},
            )
        if not isinstance(result, UpdateCheckResult):
            raise PublicHTTPError(
                502,
                "update_check_failed",
                "The update service returned an invalid result.",
            )
        payload = {
            "request_id": self._request_id(),
            "status": result.status.value,
            "available": result.status is UpdateStatus.AVAILABLE,
            "detail": result.detail,
            "update": _public_update(result),
        }
        return self.send_json(200, payload, "update_check")

    def _send_locale(self, encoded_locale: str):
        try:
            locale = unquote(encoded_locale, errors="strict")
        except UnicodeError:
            raise PublicHTTPError(
                400,
                "invalid_locale",
                "The requested locale is invalid.",
            ) from None
        if locale not in SUPPORTED_LOCALES:
            if not locale or ".." in locale or "/" in locale or "\\" in locale:
                raise PublicHTTPError(
                    403,
                    "forbidden_path",
                    "The requested resource is not available.",
                )
            raise PublicHTTPError(
                404,
                "locale_not_found",
                "The requested locale is not available.",
            )
        try:
            body = (self.locale_root / f"{locale}.json").read_bytes()
            payload = json.loads(body.decode("utf-8", errors="strict"))
            if not isinstance(payload, dict):
                raise ValueError("locale root is not an object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise PublicHTTPError(
                503,
                "capability_unavailable",
                "The requested locale is unavailable in this build.",
                {"capability": "locale", "locale": locale},
            ) from None
        return self.send_bytes(
            200,
            body,
            "application/json; charset=utf-8",
            "locale",
        )

    def _send_data(self):
        key = self.headers.get("X-Admin-Key", "").strip()
        if not key:
            raise PublicHTTPError(400, "missing_admin_key", "Enter an Admin API Key to continue.")
        try:
            validate_admin_key(key)
            client = self.client_factory(key)
            usage = fetch_usage(client, self.catalog)
            costs = fetch_costs(client, usage["start"], usage["end"])
        except OpenAIClientError as exc:
            raise PublicHTTPError(exc.http_status, exc.code, exc.message) from None
        except UsageDataError:
            raise PublicHTTPError(
                502,
                "usage_response_invalid",
                "OpenAI returned Usage data that this version cannot read.",
            ) from None
        return self.send_json(
            200,
            {
                "request_id": self._request_id(),
                "usage": usage,
                "costs": costs,
                "sources": {
                    "usage": {
                        "kind": "openai_admin_usage_api",
                        "complete": True,
                        "start": usage["start"],
                        "end": usage["end"],
                    },
                    "costs": {
                        "kind": "openai_admin_costs_api",
                        "complete": bool(costs["available"]),
                        "start": usage["start"],
                        "end": usage["end"],
                    },
                    "estimate": {
                        "kind": "catalog_list_price_estimate",
                        "catalog_version": self.catalog.get("catalog_version"),
                    },
                },
            },
            "data",
        )

    def do_OPTIONS(self):
        self.request_id = uuid.uuid4().hex
        self.send_public_error(PublicHTTPError(405, "method_not_allowed", "This method is not allowed."))


def create_server(
    port: int = 0,
    *,
    client_factory=None,
    catalog: dict | None = None,
    web_root: Path | None = None,
    locale_root: Path | None = None,
    config_service: ConfigService | None = None,
    update_checker: UpdateCheckService | None = None,
    runtime_services: RuntimeServices | None = None,
    database=None,
    credential_store=None,
    credential_verifier=None,
    project_keys=None,
    alert_state=None,
) -> ThreadingHTTPServer:
    if runtime_services is not None and any(
        item is not None
        for item in (
            config_service,
            update_checker,
            database,
            credential_store,
            credential_verifier,
            project_keys,
            alert_state,
        )
    ):
        raise ValueError(
            "runtime_services cannot be combined with individual service arguments"
        )
    configured_catalog = catalog if catalog is not None else load_catalog()
    runtime_client_factory = (
        (lambda key, _timeout: client_factory(key))
        if client_factory is not None
        else (lambda key, timeout: OpenAIAdminClient(key, timeout=timeout))
    )
    services = runtime_services or RuntimeServices(
        config_service=config_service,
        update_checker=update_checker,
        database=database,
        credential_store=credential_store,
        credential_verifier=(
            credential_verifier
            if credential_verifier is not None
            else OpenAIUsageCredentialVerifier()
        ),
        admin_client_factory=runtime_client_factory,
        project_keys=project_keys,
        alert_state=alert_state,
        catalog=configured_catalog,
    )
    if client_factory is None:
        client_factory = lambda key: OpenAIAdminClient(
            key,
            timeout=services.config.network.request_timeout_seconds,
        )
    configured_handler = type(
        "ConfiguredHandler",
        (Handler,),
        {
            "catalog": configured_catalog,
            "web_root": web_root if web_root is not None else resource_path("web"),
            "locale_root": locale_root if locale_root is not None else resource_path("locales"),
            "client_factory": staticmethod(client_factory),
            "runtime_services": services,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), configured_handler)
    server.daemon_threads = True
    return server


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    document = {}
    for key, value in pairs:
        if key in document:
            raise PublicHTTPError(
                400,
                "invalid_json",
                "The request body contains duplicate fields.",
            )
        document[key] = value
    return document


def _reject_json_constant(_value: str) -> None:
    raise PublicHTTPError(
        400,
        "invalid_json",
        "The request body contains a non-standard number.",
    )


def _public_update(result: UpdateCheckResult) -> dict | None:
    manifest = result.manifest
    if manifest is None:
        return None
    return {
        "version": str(manifest.version),
        "channel": manifest.channel,
        "published_at": manifest.published_at.isoformat().replace("+00:00", "Z"),
        "release_notes_url": manifest.release_notes_url,
        "critical": manifest.critical,
        "artifacts": [
            {
                "os": artifact.os,
                "arch": artifact.arch,
                "format": artifact.format,
                "size": artifact.size,
            }
            for artifact in manifest.artifacts
        ],
    }
