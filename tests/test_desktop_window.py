"""Desktop window launch logic (run_standalone._open_ui_window / main()).

pywebview has no GUI in CI / the Linux sandbox, so `webview` is always mocked
here and no real window is ever created.
"""
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import run_standalone

BASE = "http://127.0.0.1:8000"


def _settings(**kw):
    d = dict(
        tls_direct=False, https_required=False, HOST="127.0.0.1", PORT=8000,
        APP_NAME="AIMS Backend", docs_enabled=False, SSL_CERTFILE="", SSL_KEYFILE="",
        trusted_proxy_list=[],
    )
    d.update(kw)
    return SimpleNamespace(**d)


@pytest.fixture()
def browser(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(run_standalone.webbrowser, "open", m)
    return m


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def test_webview_import_failure_falls_back_to_browser(monkeypatch, browser, frozen):
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` -> ImportError
    assert run_standalone._open_ui_window(BASE, _settings()) is False
    browser.assert_called_once_with(BASE + "/ui")


@pytest.mark.parametrize("flag", ["tls_direct", "https_required"])
def test_tls_mode_uses_browser_and_never_touches_webview(monkeypatch, browser, frozen, flag):
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert run_standalone._open_ui_window("https://127.0.0.1:8000", _settings(**{flag: True})) is False
    browser.assert_called_once_with("https://127.0.0.1:8000/ui")
    assert fake.method_calls == [] and not fake.create_window.called


def test_source_run_uses_browser(monkeypatch, browser):
    monkeypatch.delattr(sys, "frozen", raising=False)
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert run_standalone._open_ui_window(BASE, _settings()) is False
    browser.assert_called_once_with(BASE + "/ui")
    assert not fake.create_window.called


def test_frozen_opens_desktop_window(monkeypatch, browser, frozen):
    fake = types.SimpleNamespace(
        settings={}, create_window=MagicMock(), start=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert run_standalone._open_ui_window(BASE, _settings()) is True
    args, kwargs = fake.create_window.call_args
    assert args == ("AIMS Backend", BASE + "/ui")
    assert (kwargs["width"], kwargs["height"]) == (1280, 800)
    assert "on_top" not in kwargs  # pywebview#1823 deadlock
    fake.start.assert_called_once()
    browser.assert_not_called()


def test_window_failure_falls_back_to_browser(monkeypatch, browser, frozen):
    fake = types.SimpleNamespace(
        settings={}, create_window=MagicMock(), start=MagicMock(side_effect=RuntimeError("no WebView2")),
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert run_standalone._open_ui_window(BASE, _settings()) is False
    browser.assert_called_once_with(BASE + "/ui")


def _run_main(monkeypatch, open_browser_env):
    """Run main() with the server, settings, env and health probe all faked."""
    import app.core.config as config_mod
    import uvicorn

    s = _settings()
    monkeypatch.setattr(run_standalone, "_prepare_env", lambda base: None)
    monkeypatch.setattr(sys, "argv", ["AIMS-Backend"])
    monkeypatch.setattr(config_mod, "reload_settings", lambda: s)
    monkeypatch.setattr(config_mod, "settings", config_mod.settings)  # restored afterwards
    monkeypatch.setenv("AIMS_OPEN_BROWSER", open_browser_env)

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: SimpleNamespace(args=a, kwargs=k))
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        run_standalone.urllib.request, "build_opener",
        lambda *h: SimpleNamespace(open=lambda req, timeout=None: _Resp()),
    )
    opener = MagicMock(return_value=False)
    monkeypatch.setattr(run_standalone, "_open_ui_window", opener)
    rc = run_standalone.main()
    return rc, opener


def test_open_browser_zero_does_not_open_any_ui(monkeypatch):
    rc, opener = _run_main(monkeypatch, "0")
    assert rc == 0
    opener.assert_not_called()


def test_open_browser_default_opens_ui_once(monkeypatch):
    rc, opener = _run_main(monkeypatch, "1")
    assert rc == 0
    opener.assert_called_once()
    assert opener.call_args.args[0] == BASE
