import json
import mimetypes
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cost_service import fetch_costs
from .model_catalog import load_catalog, resource_path
from .openai_client import OpenAIAdminClient, OpenAIClientError, validate_admin_key
from .usage_service import UsageDataError, fetch_usage
from .version import __version__


class PublicHTTPError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Handler(BaseHTTPRequestHandler):
    web_root = resource_path("web")
    catalog = load_catalog()
    client_factory = staticmethod(OpenAIAdminClient)

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

    def send_bytes(self, code: int, body: bytes, content_type: str, event: str):
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
        self.send_json(
            error.status,
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": self._request_id(),
                }
            },
            error.code,
        )

    def _validate_host(self) -> None:
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].lower()
        if hostname not in {"127.0.0.1", "localhost"}:
            raise PublicHTTPError(403, "invalid_host", "This local server only accepts loopback requests.")

    def _static_path(self, raw_path: str) -> Path:
        try:
            decoded = unquote(raw_path, errors="strict").replace("\\", "/")
        except UnicodeError:
            raise PublicHTTPError(400, "invalid_path", "The requested path is invalid.") from None
        segments = decoded.split("/")
        if "\x00" in decoded or any(segment == ".." for segment in segments):
            raise PublicHTTPError(403, "forbidden_path", "The requested resource is not available.")
        filename = "index.html" if decoded == "/" else decoded.lstrip("/")
        root = self.web_root.resolve()
        file_path = (root / filename).resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            raise PublicHTTPError(403, "forbidden_path", "The requested resource is not available.") from None
        return file_path

    def do_GET(self):
        self.request_id = uuid.uuid4().hex
        try:
            self._validate_host()
            path = urlsplit(self.path).path
            if path == "/api/catalog":
                return self.send_json(200, {**self.catalog, "version": __version__}, "catalog")
            if path == "/api/data":
                return self._send_data()
            file_path = self._static_path(path)
            if not file_path.is_file():
                raise PublicHTTPError(404, "not_found", "The requested resource was not found.")
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            return self.send_bytes(200, file_path.read_bytes(), content_type, "static")
        except PublicHTTPError as exc:
            return self.send_public_error(exc)
        except Exception:
            return self.send_public_error(
                PublicHTTPError(500, "internal_error", "The local server could not complete the request.")
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
            {"request_id": self._request_id(), "usage": usage, "costs": costs},
            "data",
        )

    def do_OPTIONS(self):
        self.request_id = uuid.uuid4().hex
        self.send_public_error(PublicHTTPError(405, "method_not_allowed", "This method is not allowed."))


def create_server(
    port: int = 0,
    *,
    client_factory=OpenAIAdminClient,
    catalog: dict | None = None,
    web_root: Path | None = None,
) -> ThreadingHTTPServer:
    configured_handler = type(
        "ConfiguredHandler",
        (Handler,),
        {
            "catalog": catalog if catalog is not None else load_catalog(),
            "web_root": web_root if web_root is not None else resource_path("web"),
            "client_factory": staticmethod(client_factory),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), configured_handler)
    server.daemon_threads = True
    return server
