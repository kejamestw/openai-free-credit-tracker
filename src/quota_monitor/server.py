import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .cost_service import fetch_costs
from .model_catalog import load_catalog, resource_path
from .openai_client import OpenAIAdminClient
from .usage_service import fetch_usage


class Handler(BaseHTTPRequestHandler):
    web_root = resource_path("web")
    catalog = load_catalog()

    def log_message(self, *_):
        return

    def send_bytes(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, payload: dict):
        self.send_bytes(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/catalog":
            return self.send_json(200, self.catalog)
        if path == "/api/data":
            try:
                key = self.headers.get("X-Admin-Key", "").strip()
                client = OpenAIAdminClient(key)
                usage = fetch_usage(client, self.catalog)
                costs = fetch_costs(client, usage["start"], usage["end"])
                return self.send_json(200, {"usage": usage, "costs": costs})
            except Exception as exc:
                return self.send_json(400, {"error": {"message": str(exc)}})
        filename = "index.html" if path == "/" else path.lstrip("/")
        file_path = (self.web_root / filename).resolve()
        if self.web_root.resolve() not in file_path.parents and file_path != self.web_root.resolve():
            return self.send_json(403, {"error": {"message": "Forbidden"}})
        if not file_path.is_file():
            return self.send_json(404, {"error": {"message": "Not found"}})
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_bytes(200, file_path.read_bytes(), content_type)


def create_server(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
