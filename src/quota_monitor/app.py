import argparse

from .config_service import ConfigService, default_config
from .desktop_integration import build_desktop_composition
from .desktop_runtime import DesktopStartMode
from .i18n import load_locale_directory
from .model_catalog import resource_path
from .operations_cli import add_operations_subparsers, run_operation
from .platform_paths import AppPaths, resolve_app_paths
from .runtime import RuntimeServices
from .server import create_server
from .version import __version__


REQUIRED_RESOURCES = (
    ("data", "models.json"),
    ("web", "index.html"),
    ("web", "css", "app.css"),
    ("web", "js", "app.js"),
    ("web", "js", "domain.js"),
    ("locales", "en.json"),
    ("locales", "zh-TW.json"),
)


def validate_resources() -> None:
    missing = ["/".join(parts) for parts in REQUIRED_RESOURCES if not resource_path(*parts).is_file()]
    if missing:
        raise RuntimeError(f"missing bundled resources: {', '.join(missing)}")
    load_locale_directory(resource_path("locales"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local OpenAI Free Credit Tracker server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="validate bundled resources and loopback binding, then exit",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the dashboard in the default browser",
    )
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--config-path",
        action="store_true",
        help="print the resolved configuration file path and exit",
    )
    parser.add_argument(
        "--data-path",
        action="store_true",
        help="print the resolved application data directory and exit",
    )
    parser.add_argument(
        "--log-path",
        action="store_true",
        help="print the resolved log directory and exit",
    )
    add_operations_subparsers(parser)
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    paths = resolve_app_paths()
    if args.config_path or args.data_path or args.log_path:
        _print_requested_paths(args, paths)
        return
    if args.operation is not None:
        paths.ensure_directories()
        return run_operation(args, paths)
    validate_resources()
    if args.smoke_test:
        runtime_services = RuntimeServices(
            paths=paths,
            config_service=ConfigService(paths),
            initial_config=default_config(),
        )
        server = create_server(runtime_services=runtime_services)
        host, _ = server.server_address
        try:
            if host != "127.0.0.1":
                raise RuntimeError(f"unsafe bind address: {host}")
            print(f"OpenAI Free Credit Tracker {__version__} smoke test passed")
        finally:
            server.server_close()
        return
    paths.ensure_directories()
    composition = build_desktop_composition(
        paths,
        no_browser=args.no_browser,
        background=args.background,
    )
    try:
        mode = composition.desktop.start()
        if mode is DesktopStartMode.SECONDARY_ACTIVATED:
            return 0
        print(f"OpenAI Free Credit Tracker: {composition.server.base_url}")
        if mode is DesktopStartMode.TRAY:
            print("Use the tray Exit action to stop the local server.")
        else:
            print("Press Ctrl-C to stop the local server.")
        config_result = composition.data_runtime.config_result
        if config_result.warning:
            print(f"Configuration warning: {config_result.warning}")
            print(f"Configuration file: {config_result.config_path}")
        composition.desktop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        composition.desktop.shutdown()
    return 0


def _print_requested_paths(args: argparse.Namespace, paths: AppPaths) -> None:
    requested = []
    if args.config_path:
        requested.append(("config", paths.config_file))
    if args.data_path:
        requested.append(("data", paths.data_dir))
    if args.log_path:
        requested.append(("log", paths.log_dir))
    if len(requested) == 1:
        print(requested[0][1])
        return
    for label, path in requested:
        print(f"{label}: {path}")
