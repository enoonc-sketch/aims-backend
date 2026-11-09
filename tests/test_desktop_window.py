"""Desktop-window fallback behavior without requiring a GUI."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


SPEC = importlib.util.spec_from_file_location(
    "run_standalone_under_test", Path(__file__).resolve().parents[1] / "run_standalone.py"
)
standalone = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(standalone)


def _frozen(monkeypatch):
    monkeypatch.setattr(standalone.sys, "frozen", True, raising=False)
    monkeypatch.setattr(standalone, "webbrowser", SimpleNamespace(open=Mock(return_value=True)))


def test_missing_webview_falls_back_to_browser(monkeypatch):
    _frozen(monkeypatch)
    monkeypatch.setitem(sys.modules, "webview", None)

    assert standalone._open_ui_window("http://127.0.0.1:8000", SimpleNamespace()) is False
    standalone.webbrowser.open.assert_called_once_with("http://127.0.0.1:8000/ui")


def test_window_creation_failure_falls_back_to_browser(monkeypatch):
    _frozen(monkeypatch)
    webview = SimpleNamespace(settings={}, create_window=Mock(side_effect=RuntimeError("no GUI")))
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert standalone._open_ui_window("http://127.0.0.1:8000", SimpleNamespace()) is False
    standalone.webbrowser.open.assert_called_once_with("http://127.0.0.1:8000/ui")


def test_successful_window_returns_true_without_browser(monkeypatch):
    _frozen(monkeypatch)
    webview = SimpleNamespace(settings={}, create_window=Mock(), start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert standalone._open_ui_window("http://127.0.0.1:8000", SimpleNamespace()) is True
    webview.create_window.assert_called_once()
    webview.start.assert_called_once_with()
    standalone.webbrowser.open.assert_not_called()


def test_tls_and_source_mode_use_browser(monkeypatch):
    _frozen(monkeypatch)
    monkeypatch.delitem(sys.modules, "webview", raising=False)

    assert standalone._open_ui_window(
        "https://127.0.0.1:8000", SimpleNamespace(tls_direct=True)
    ) is False
    monkeypatch.delattr(standalone.sys, "frozen")
    assert standalone._open_ui_window("http://127.0.0.1:8000", SimpleNamespace()) is False
    assert standalone.webbrowser.open.call_count == 2


class _HealthResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _HealthOpener:
    def open(self, req, timeout=None):
        return _HealthResponse()


def test_open_browser_zero_never_opens_window(monkeypatch, tmp_path):
    """AIMS_OPEN_BROWSER=0: main() starts the server and never opens a window."""
    monkeypatch.setattr(standalone, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(standalone, "_prepare_env", lambda base: None)
    monkeypatch.setattr(standalone.sys, "argv", ["run_standalone.py"])
    monkeypatch.setenv("AIMS_OPEN_BROWSER", "0")
    monkeypatch.setattr(standalone, "_open_ui_window", Mock())

    settings = SimpleNamespace(
        HOST="127.0.0.1",
        PORT=8000,
        tls_direct=False,
        https_required=False,
        APP_NAME="AIMS Backend",
        docs_enabled=False,
        trusted_proxy_list=[],
    )
    config_mod = SimpleNamespace(reload_settings=lambda: settings)
    core_mod = SimpleNamespace(config=config_mod)
    app_mod = SimpleNamespace(core=core_mod)
    main_mod = SimpleNamespace(app=object())
    app_mod.main = main_mod
    monkeypatch.setitem(sys.modules, "app", app_mod)
    monkeypatch.setitem(sys.modules, "app.core", core_mod)
    monkeypatch.setitem(sys.modules, "app.core.config", config_mod)
    monkeypatch.setitem(sys.modules, "app.main", main_mod)
    server = SimpleNamespace(should_exit=False, run=lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=lambda *a, **k: object(), Server=lambda cfg: server),
    )
    monkeypatch.setattr(
        standalone.urllib.request, "build_opener", lambda *a: _HealthOpener()
    )

    assert standalone.main() == 0
    standalone._open_ui_window.assert_not_called()
