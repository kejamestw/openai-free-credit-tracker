import argparse
import threading
import webbrowser

from .model_catalog import resource_path
from .server import create_server
from .version import __version__


REQUIRED_RESOURCES = (
    ("data", "models.json"),
    ("web", "index.html"),
    ("web", "css", "app.css"),
    ("web", "js", "app.js"),
    ("web", "js", "domain.js"),
)


def validate_resources() -> None:
    missing = ["/".join(parts) for parts in REQUIRED_RESOURCES if not resource_path(*parts).is_file()]
    if missing:
        raise RuntimeError(f"missing bundled resources: {', '.join(missing)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local OpenAI Free Credit Tracker server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="validate bundled resources and loopback binding, then exit",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    validate_resources()
    server = create_server()
    if args.smoke_test:
        host, _ = server.server_address
        try:
            if host != "127.0.0.1":
                raise RuntimeError(f"unsafe bind address: {host}")
            print(f"OpenAI Free Credit Tracker {__version__} smoke test passed")
        finally:
            server.server_close()
        return
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"OpenAI Free Credit Tracker: {url}")
    print("Close this window to stop the local server.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
