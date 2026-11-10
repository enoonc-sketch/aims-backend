"""Desktop-window launcher (run_standalone._open_ui_window / main), 0.5.4.

pywebview has no GUI in CI/sandbox, so the `webview` module is always faked here.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def rs():
    spec = importlib.util.spec_from_file_location("run_standalone_under_test", _ROOT / "run_standalone.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settings(tls_direct=False, https_required=False):
    return types.SimpleNamespace(tls_direct=tls_direct, https_required=https_required)


def test_missing_webview_falls_back_to_browser(rs, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` -> ImportError
    opened = []
    monkeypatch.setattr(rs.webbrowser, "open", lambda url, *a, **k: opened.append(url))

    assert rs._open_ui_window("http://127.0.0.1:8000", _settings()) is False
    assert opened == ["http://127.0.0.1:8000/ui"]


def test_tls_mode_never_imports_webview(rs, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    class _Boom(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError("webview must not be used in TLS mode")

    monkeypatch.setitem(sys.modules, "webview", _Boom("webview"))
    opened = []
    monkeypatch.setattr(rs.webbrowser, "open", lambda url, *a, **k: opened.append(url))

    for st in (_settings(tls_direct=True), _settings(https_required=True)):
        opened.clear()
        assert rs._open_ui_window("https://127.0.0.1:8000", st) is False
        assert opened == ["https://127.0.0.1:8000/ui"]


def test_source_run_uses_browser_not_window(rs, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: pytest.fail("window must not open from source")
    monkeypatch.setitem(sys.modules, "webview", fake)
    opened = []
    monkeypatch.setattr(rs.webbrowser, "open", lambda url, *a, **k: opened.append(url))

    assert rs._open_ui_window("http://127.0.0.1:8000", _settings()) is False
    assert opened == ["http://127.0.0.1:8000/ui"]


def test_frozen_exe_opens_window(rs, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = {}
    fake = types.ModuleType("webview")
    fake.settings = {}
    fake.create_window = lambda title, url, **kw: calls.update(title=title, url=url, kw=kw)
    fake.start = lambda *a, **k: calls.update(started=True)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(rs.webbrowser, "open", lambda *a, **k: pytest.fail("browser must not open"))

    assert rs._open_ui_window("http://127.0.0.1:8000", _settings()) is True
    assert calls["url"] == "http://127.0.0.1:8000/ui" and calls["started"]
    assert calls["kw"]["width"] == 1280 and calls["kw"]["height"] == 800
    assert "on_top" not in calls["kw"]  # pywebview 6.2.1 EdgeChromium deadlock, see code comment
    assert fake.settings.get("ALLOW_DOWNLOADS") is True


def test_window_failure_falls_back_to_browser(rs, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake = types.ModuleType("webview")
    fake.settings = {}
    fake.create_window = lambda *a, **k: None

    def _boom(*a, **k):
        raise RuntimeError("WebView2 Runtime missing")

    fake.start = _boom
    monkeypatch.setitem(sys.modules, "webview", fake)
    opened = []
    monkeypatch.setattr(rs.webbrowser, "open", lambda url, *a, **k: opened.append(url))

    assert rs._open_ui_window("http://127.0.0.1:8000", _settings()) is False
    assert opened == ["http://127.0.0.1:8000/ui"]


def _wire_main(rs, monkeypatch, *, health, server_dies=False):
    """Common fakes for driving rs.main() without a real server.

    health: JSON dict the port answers BEFORE our server runs (None = nothing listening).
    server_dies: our fake uvicorn exits immediately (like a bind error) instead of serving.
    Returns a dict collecting what happened.
    """
    import json

    import uvicorn
    import app.core.config as config_mod

    monkeypatch.setattr(config_mod, "settings", config_mod.settings)  # restore after main() reassigns it
    monkeypatch.setattr(sys, "argv", ["run_standalone.py"])
    monkeypatch.setattr(rs, "_prepare_env", lambda base: None)
    monkeypatch.setattr(rs, "_backup_database", lambda *a, **k: None)
    monkeypatch.setattr(rs, "_hold_app_mutex", lambda *a, **k: None)
    monkeypatch.setattr(rs, "_setup_file_logging", lambda *a, **k: None)
    seen = {"ran": 0, "opened": []}

    class _FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            seen["ran"] += 1
            if server_dies:
                return
            # Like real uvicorn it serves until asked to stop; the cap only keeps the test
            # finite, because with no window main() blocks here until Ctrl+C.
            end = rs.time.time() + 1.0
            while not self.should_exit and rs.time.time() < end:
                rs.time.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    class _Resp:
        status = 200

        def __init__(self, payload):
            self._p = payload

        def read(self):
            return json.dumps(self._p).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            if seen["ran"] and not server_dies:
                return _Resp({"app": config_mod.settings.APP_NAME})
            if not seen["ran"] and health is not None:
                return _Resp(health)
            raise OSError("connection refused")

    monkeypatch.setattr(rs.urllib.request, "build_opener", lambda *h: _Opener())
    monkeypatch.setattr(rs, "_open_ui_window", lambda base_url, st: seen["opened"].append(base_url) or True)
    return seen


def test_open_browser_zero_skips_ui(rs, monkeypatch):
    """AIMS_OPEN_BROWSER=0 => serve only: _open_ui_window must not be called."""
    monkeypatch.setenv("AIMS_OPEN_BROWSER", "0")
    seen = _wire_main(rs, monkeypatch, health=None)

    assert rs.main() == 0
    assert seen["ran"] == 1 and seen["opened"] == []


def test_second_launch_reuses_running_instance(rs, monkeypatch):
    """Double-click twice: no second server, just a window onto the running one."""
    import app.core.config as config_mod

    monkeypatch.setenv("AIMS_OPEN_BROWSER", "1")
    seen = _wire_main(rs, monkeypatch, health={"app": config_mod.settings.APP_NAME})

    assert rs.main() == 0
    assert seen["ran"] == 0  # our server was never started
    assert len(seen["opened"]) == 1 and seen["opened"][0].startswith("http://127.0.0.1:")


def test_port_used_by_other_service_aborts(rs, monkeypatch):
    monkeypatch.setenv("AIMS_OPEN_BROWSER", "1")
    seen = _wire_main(rs, monkeypatch, health={"app": "SomethingElse"})

    assert rs.main() == 1
    assert seen["ran"] == 0 and seen["opened"] == []


def test_server_dying_early_fails_fast(rs, monkeypatch, capsys):
    """uvicorn exits at once (e.g. bind error): report it immediately, don't wait 15 s."""
    monkeypatch.setenv("AIMS_OPEN_BROWSER", "1")
    seen = _wire_main(rs, monkeypatch, health=None, server_dies=True)

    t0 = rs.time.time()
    assert rs.main() == 1
    assert rs.time.time() - t0 < 5
    assert "already in use" in capsys.readouterr().out
    assert seen["opened"] == []


def test_crash_log_falls_back_when_install_dir_is_read_only(rs, tmp_path, monkeypatch):
    """Under Program Files the EXE folder is read-only: the log must land in aims_data."""
    (tmp_path / "aims_data").mkdir()
    (tmp_path / "aims_crash.log").mkdir()  # makes open(base/aims_crash.log, "w") fail
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        path = rs._write_crash_log(tmp_path, exc)
    assert path == tmp_path / "aims_data" / "aims_crash.log"
    assert "boom" in path.read_text(encoding="utf-8")
