from types import SimpleNamespace

from quota_monitor import app
from quota_monitor.desktop_runtime import DesktopStartMode
from quota_monitor.platform_paths import AppPaths


def make_paths(tmp_path):
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
    )


def test_path_diagnostics_print_and_exit_without_creating_directories(
    monkeypatch, tmp_path, capsys
):
    paths = make_paths(tmp_path)
    monkeypatch.setattr(app, "resolve_app_paths", lambda: paths)
    monkeypatch.setattr(
        app,
        "validate_resources",
        lambda: (_ for _ in ()).throw(AssertionError("must not validate resources")),
    )

    app.main(["--config-path", "--data-path", "--log-path"])

    assert capsys.readouterr().out.splitlines() == [
        f"config: {paths.config_file}",
        f"data: {paths.data_dir}",
        f"log: {paths.log_dir}",
    ]
    assert not paths.config_dir.exists()
    assert not paths.data_dir.exists()
    assert not paths.log_dir.exists()


class FakeHttpServer:
    server_address = ("127.0.0.1", 12345)

    def __init__(self):
        self.closed = False

    def server_close(self):
        self.closed = True


def test_smoke_test_does_not_read_or_create_user_directories(monkeypatch, tmp_path):
    paths = make_paths(tmp_path)
    server = FakeHttpServer()
    monkeypatch.setattr(app, "resolve_app_paths", lambda: paths)
    monkeypatch.setattr(app, "validate_resources", lambda: None)
    monkeypatch.setattr(app, "create_server", lambda **_kwargs: server)

    assert app.main(["--smoke-test"]) is None

    assert server.closed is True
    assert all(
        not path.exists()
        for path in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir)
    )


class FakeDesktop:
    def __init__(self, mode=DesktopStartMode.FOREGROUND):
        self.mode = mode
        self.started = False
        self.waited = False
        self.stopped = False

    def start(self):
        self.started = True
        return self.mode

    def wait(self):
        self.waited = True

    def shutdown(self):
        self.stopped = True


def run_desktop_app(monkeypatch, paths, argv, *, mode=DesktopStartMode.FOREGROUND):
    desktop = FakeDesktop(mode)
    composition = SimpleNamespace(
        desktop=desktop,
        server=SimpleNamespace(base_url="http://127.0.0.1:12345"),
        data_runtime=SimpleNamespace(
            config_result=SimpleNamespace(
                warning=None,
                config_path=paths.config_file,
            )
        ),
    )
    calls = []
    monkeypatch.setattr(app, "resolve_app_paths", lambda: paths)
    monkeypatch.setattr(app, "validate_resources", lambda: None)
    monkeypatch.setattr(
        app,
        "build_desktop_composition",
        lambda received, **kwargs: (calls.append((received, kwargs)) or composition),
    )

    result = app.main(argv)
    return result, desktop, calls


def test_main_runs_desktop_lifecycle_and_forwards_browser_flags(
    monkeypatch, tmp_path, capsys
):
    paths = make_paths(tmp_path)

    result, desktop, calls = run_desktop_app(
        monkeypatch, paths, ["--no-browser", "--background"]
    )

    assert result == 0
    assert desktop.started and desktop.waited and desktop.stopped
    assert calls == [(paths, {"no_browser": True, "background": True})]
    assert "http://127.0.0.1:12345" in capsys.readouterr().out
    assert all(
        path.is_dir()
        for path in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir)
    )


def test_secondary_instance_activates_primary_then_exits_normally(monkeypatch, tmp_path):
    paths = make_paths(tmp_path)

    result, desktop, _calls = run_desktop_app(
        monkeypatch,
        paths,
        [],
        mode=DesktopStartMode.SECONDARY_ACTIVATED,
    )

    assert result == 0
    assert desktop.started and desktop.stopped
    assert desktop.waited is False


def test_keyboard_interrupt_still_performs_clean_shutdown(monkeypatch, tmp_path):
    paths = make_paths(tmp_path)
    result, desktop, _calls = run_desktop_app(monkeypatch, paths, [])
    desktop.waited = False
    desktop.wait = lambda: (_ for _ in ()).throw(KeyboardInterrupt())

    # Re-run with the same fake composition, now simulating Ctrl-C from wait().
    monkeypatch.setattr(
        app,
        "build_desktop_composition",
        lambda _paths, **_kwargs: SimpleNamespace(
            desktop=desktop,
            server=SimpleNamespace(base_url="http://127.0.0.1:12345"),
            data_runtime=SimpleNamespace(
                config_result=SimpleNamespace(warning=None, config_path=paths.config_file)
            ),
        ),
    )

    assert app.main([]) == 0
    assert result == 0
    assert desktop.stopped is True
