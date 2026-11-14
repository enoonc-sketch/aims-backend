"""托盘行为测试：关闭到托盘、托盘菜单、快捷键窗口、全局热键。

pywebview 在 CI/sandbox 里没有，这里用假模块；托盘本身是 ctypes 实现（tray_win32），菜单模型可直接测（与
tests/test_desktop_window.py 同款做法），只测启动器自己的接线逻辑。
"""
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from app.launcher import main as launcher_main
from app.launcher import platform_win as win

App = launcher_main.App


# --------------------------------------------------------------------------- #
# 假 GUI 模块
# --------------------------------------------------------------------------- #
class _FakeMenuItem:
    def __init__(self, text, action=None, default=False, checked=None, enabled=True):
        self.enabled = enabled
        self.text = text
        self.action = action
        self.default = default
        self.checked = checked


def _fake_pystray(monkeypatch):
    """历史遗留的名字：托盘已不依赖 pystray，这里什么都不用假了（保留调用点，改动最小）。"""


class _FakeEvent:
    """pywebview 的 events.closing：支持 `+= 处理函数`（不是普通 list）。"""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __len__(self):
        return len(self.handlers)

    def __getitem__(self, i):
        return self.handlers[i]


class _FakeWindow:
    def __init__(self):
        self.events = types.SimpleNamespace(closing=_FakeEvent())
        self.hidden = 0
        self.shown = 0
        self.destroyed = 0
        self.loaded = []

    def hide(self):
        self.hidden += 1

    def show(self):
        self.shown += 1

    def destroy(self):
        self.destroyed += 1

    def load_url(self, url):
        self.loaded.append(url)


def _fake_webview(monkeypatch):
    mod = types.ModuleType("webview")
    mod.settings = {}
    created = {}

    def create_window(title, url, **kw):
        w = _FakeWindow()
        created.update(window=w, title=title, url=url, kw=kw)
        return w

    mod.create_window = create_window
    mod.start = lambda *a, **k: created.update(started=True)
    monkeypatch.setitem(sys.modules, "webview", mod)
    return mod, created


class _FakeIcon:
    def __init__(self):
        self.notified = []
        self.stopped = False

    def notify(self, text, title):
        self.notified.append((title, text))

    def stop(self):
        self.stopped = True


class _FakeSupervisor:
    layout = types.SimpleNamespace(data_dir=Path("/tmp/aims-data"),
                                   logs_dir=Path("/tmp/aims-logs"))

    def __init__(self):
        self.shutdown_called = False

    def ui_url(self):
        return "http://127.0.0.1:8000/ui"

    def shutdown(self):
        self.shutdown_called = True


def _bare_app(**attrs):
    app = App.__new__(App)  # 不跑 __init__（避免建托盘/日志）
    app._quitting = False
    app._window_visible = False
    app.window = None
    app.icon = None
    app._hotkey_listener = None
    app._ui_state = None
    app._exit = threading.Event()
    for k, v in attrs.items():
        setattr(app, k, v)
    return app


# --------------------------------------------------------------------------- #
# 热键解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("ctrl+alt+a", (0x0002 | 0x0001, 0x41)),
    ("CTRL+ALT+A", (0x0002 | 0x0001, 0x41)),
    ("ctrl+shift+f9", (0x0002 | 0x0004, 0x78)),
    ("alt+f12", (0x0001, 0x7B)),
    ("win+x", (0x0008, 0x58)),
    ("ctrl+1", (0x0002, 0x31)),
])
def test_parse_hotkey_valid(text, expected):
    assert win.parse_hotkey(text) == expected


@pytest.mark.parametrize("text", [
    "", "a", "ctrl", "ctrl+alt", "ctrl+alt+zzz", "ctrl+alt+a+b",
    "meta+a", "ctrl++a", "ctrl+alt+1+2",
])
def test_parse_hotkey_invalid(text):
    assert win.parse_hotkey(text) is None


def test_format_hotkey_round_trip():
    assert win.format_hotkey(*win.parse_hotkey("ctrl+alt+a")) == "ctrl+alt+a"
    assert win.format_hotkey(*win.parse_hotkey("ctrl+shift+f9")) == "ctrl+shift+f9"


def test_hotkey_listener_noop_off_windows():
    if win.IS_WINDOWS:
        pytest.skip("only meaningful off Windows")
    assert win.HotkeyListener((0x0003, 0x41), lambda: None).start() is False


# --------------------------------------------------------------------------- #
# 启动器偏好（aims_data/launcher.json）
# --------------------------------------------------------------------------- #
def test_launcher_prefs_defaults_and_round_trip(tmp_path):
    path = tmp_path / "launcher.json"
    prefs = launcher_main.load_launcher_prefs(path)
    assert prefs == {"close_to_tray_notified": False,
                     "toggle_hotkey": launcher_main.DEFAULT_TOGGLE_HOTKEY}
    prefs["close_to_tray_notified"] = True
    prefs["toggle_hotkey"] = "ctrl+alt+b"
    assert launcher_main.save_launcher_prefs(path, prefs) is True
    assert launcher_main.load_launcher_prefs(path) == prefs


def test_launcher_prefs_bad_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "launcher.json"
    path.write_text("{not json", encoding="utf-8")
    assert launcher_main.load_launcher_prefs(path)["toggle_hotkey"] == \
        launcher_main.DEFAULT_TOGGLE_HOTKEY
    path.write_text(json.dumps({"toggle_hotkey": "a", "close_to_tray_notified": "yes"}),
                    encoding="utf-8")
    prefs = launcher_main.load_launcher_prefs(path)
    assert prefs["toggle_hotkey"] == launcher_main.DEFAULT_TOGGLE_HOTKEY
    assert prefs["close_to_tray_notified"] is False


def test_apply_hotkey_persists_and_validates(tmp_path):
    app = _bare_app(prefs_path=tmp_path / "launcher.json",
                    prefs={"close_to_tray_notified": False,
                           "toggle_hotkey": launcher_main.DEFAULT_TOGGLE_HOTKEY})
    assert app._apply_hotkey("ctrl+alt+b") is True
    assert json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))[
        "toggle_hotkey"] == "ctrl+alt+b"
    assert app._apply_hotkey("a") is False
    assert json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))[
        "toggle_hotkey"] == "ctrl+alt+b"


# --------------------------------------------------------------------------- #
# 托盘菜单
# --------------------------------------------------------------------------- #
def test_menu_items_have_settings_shortcuts_quit_and_keep_existing(monkeypatch, tmp_path):
    _fake_pystray(monkeypatch)
    app = _bare_app(root=tmp_path, supervisor=_FakeSupervisor())
    from app.launcher.tray_win32 import Menu
    texts = ["SEPARATOR" if item is Menu.SEPARATOR else item.text for item in app.menu_items()]
    # 前三项是登录信息（登录状态 / 登录人 / 使用权限），文字是函数，每次打开菜单现取
    assert [t(None) if callable(t) else t for t in texts[:3]] == ["登录状态：未获取", "登录人：—", "使用权限：—"]
    texts = texts[4:]    # 跳过三项登录信息 + 分隔线
    assert texts == ["打开 AIMS 主界面", "设置", "用户快捷键", "立即备份数据库",
                     "打开数据目录", "打开日志目录", "生成诊断报告", "重启服务",
                     "开机自动启动", "SEPARATOR", "版本 1.0.10", "退出程序"]
    assert "退出" not in texts  # 老的“退出”已改名“退出程序”，不要两个退出入口


def test_settings_url_points_at_settings_tab():
    app = _bare_app(supervisor=_FakeSupervisor())
    assert app._settings_url() == "http://127.0.0.1:8000/ui#sys/settings"


# --------------------------------------------------------------------------- #
# 关闭到托盘
# --------------------------------------------------------------------------- #
def test_desktop_window_close_hides_instead_of_quitting(monkeypatch):
    _fake_webview(monkeypatch)
    called = []
    created_window = []

    win.run_desktop_window("http://127.0.0.1:8000/ui",
                           on_close_to_tray=lambda: called.append(1),
                           on_window=created_window.append)
    window = created_window[0]
    assert len(window.events.closing) == 1
    assert window.events.closing[0]() is False  # 返回 False = 取消关闭
    assert window.hidden == 1 and called == [1]


def test_desktop_window_close_allowed_while_quitting(monkeypatch):
    _fake_webview(monkeypatch)
    called = []
    created_window = []

    win.run_desktop_window("http://127.0.0.1:8000/ui",
                           on_close_to_tray=lambda: called.append(1),
                           on_window=created_window.append,
                           is_quitting=lambda: True)
    window = created_window[0]
    assert window.events.closing[0]() is None  # 正在退出：放行，不隐藏、不通知
    assert window.hidden == 0 and called == []


def test_desktop_window_without_tray_callback_closes_normally(monkeypatch):
    _fake_webview(monkeypatch)
    created_window = []
    win.run_desktop_window("http://127.0.0.1:8000/ui",
                           on_window=created_window.append)
    assert len(created_window[0].events.closing) == 0


def test_close_to_tray_notifies_only_first_time(tmp_path):
    icon = _FakeIcon()
    app = _bare_app(prefs_path=tmp_path / "launcher.json",
                    prefs={"close_to_tray_notified": False,
                           "toggle_hotkey": launcher_main.DEFAULT_TOGGLE_HOTKEY},
                    icon=icon, _window_visible=True)
    app._on_close_to_tray()
    assert app._window_visible is False
    assert len(icon.notified) == 1
    assert "仍在后台运行" in icon.notified[0][1]
    assert json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))[
        "close_to_tray_notified"] is True
    app._on_close_to_tray()
    assert len(icon.notified) == 1  # 第二次关窗口不再弹


def test_menu_quit_destroys_window_and_stops_icon():
    icon = _FakeIcon()
    sup = _FakeSupervisor()
    window = _FakeWindow()
    app = _bare_app(icon=icon, supervisor=sup, window=window)
    app._menu_quit()
    assert app._quitting is True and app._exit.is_set()
    assert window.destroyed == 1
    assert icon.stopped is True
    assert sup.shutdown_called is True


# --------------------------------------------------------------------------- #
# 显示/隐藏主窗口
# --------------------------------------------------------------------------- #
def test_toggle_main_window_hides_and_shows():
    window = _FakeWindow()
    app = _bare_app(window=window, _window_visible=True)
    app._toggle_main_window()
    assert window.hidden == 1 and app._window_visible is False
    app._toggle_main_window()
    assert window.shown == 1 and app._window_visible is True


def test_open_ui_reuses_window_or_opens_browser(monkeypatch):
    window = _FakeWindow()
    app = _bare_app(window=window, supervisor=_FakeSupervisor())
    monkeypatch.setattr(win, "open_app_window",
                        lambda url: pytest.fail("should reuse the window"))
    app._menu_open_ui()
    assert window.shown == 1

    app.window = None
    opened = []
    monkeypatch.setattr(win, "open_app_window", lambda url: opened.append(url))
    app._menu_open_ui()
    assert opened == ["http://127.0.0.1:8000/ui"]


def test_open_settings_reuses_window_with_deep_link(monkeypatch):
    window = _FakeWindow()
    app = _bare_app(window=window, supervisor=_FakeSupervisor())
    monkeypatch.setattr(win, "open_app_window",
                        lambda url: pytest.fail("should reuse the window"))
    app._menu_open_settings()
    assert window.shown == 1
    assert window.loaded == ["http://127.0.0.1:8000/ui#sys/settings"]


# --------------------------------------------------------------------------- #
# 托盘里的登录信息：登录状态 / 登录人 / 使用权限
# --------------------------------------------------------------------------- #
def test_tray_login_items_are_read_only_and_come_first(monkeypatch, tmp_path):
    _fake_pystray(monkeypatch)
    app = _bare_app(root=tmp_path, supervisor=_FakeSupervisor())
    items = list(app.menu_items())
    assert all(i.enabled is False for i in items[:3])
    from app.launcher.tray_win32 import Menu
    assert items[3] is Menu.SEPARATOR


def test_tray_login_texts_follow_ui_state():
    app = _bare_app(supervisor=_FakeSupervisor())
    app._ui_state = {"logged_in": True, "username": "ops", "full_name": "张工", "permission": "Engineer"}
    assert app._login_status_text() == "登录状态：已登录"
    assert app._login_user_text() == "登录人：ops（张工）"
    assert app._login_permission_text() == "使用权限：Engineer"
    assert "已登录：ops" in app._tray_title()
    app._ui_state = {"logged_in": False}
    assert app._login_status_text() == "登录状态：未登录"
    assert app._login_user_text() == "登录人：—" and app._login_permission_text() == "使用权限：—"
    assert app._tray_title().endswith("未登录")
