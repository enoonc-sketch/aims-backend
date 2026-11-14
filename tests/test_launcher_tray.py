"""托盘行为测试：关闭到托盘、托盘菜单、快捷键窗口、全局热键。

托盘本身是 ctypes 实现（tray_win32），菜单模型可直接测（与
tests/test_desktop_window.py 同款做法），只测启动器自己的接线逻辑。
界面只用系统自带的 Edge --app 窗口，不再有内嵌原生窗口。
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
    app.icon = None
    app._hotkey_listener = None
    app._ui_state = None
    app._alarms = None
    app._alarm_baseline = None
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
                     "toggle_hotkey": launcher_main.DEFAULT_TOGGLE_HOTKEY,
                     "alarm_notify": True}
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
def test_menu_items_have_settings_shortcuts_quit_and_keep_existing(tmp_path):
    app = _bare_app(root=tmp_path, supervisor=_FakeSupervisor())
    from app.launcher.tray_win32 import Menu
    texts = ["SEPARATOR" if item is Menu.SEPARATOR else item.text for item in app.menu_items()]
    # 前三项是登录信息（登录状态 / 登录人 / 使用权限），文字是函数，每次打开菜单现取
    assert [t(None) if callable(t) else t for t in texts[:3]] == ["登录状态：未获取", "登录人：—", "使用权限：—"]
    texts = texts[4:]    # 跳过三项登录信息 + 分隔线
    texts = [t(None) if callable(t) else t for t in texts]
    assert texts == ["当前报警：未获取", "SEPARATOR", "打开 AIMS 主界面", "设置", "用户快捷键",
                     "立即备份数据库", "打开数据目录", "打开日志目录", "生成诊断报告", "重启服务",
                     "开机自动启动", "新报警弹出气泡通知", "SEPARATOR", "版本 1.0.12", "退出程序"]
    assert "退出" not in texts  # 老的“退出”已改名“退出程序”，不要两个退出入口


def test_settings_url_points_at_settings_tab():
    app = _bare_app(supervisor=_FakeSupervisor())
    assert app._settings_url() == "http://127.0.0.1:8000/ui#sys/settings"


# --------------------------------------------------------------------------- #
# 关闭到托盘
# --------------------------------------------------------------------------- #
def test_close_to_tray_notifies_only_first_time(tmp_path):
    icon = _FakeIcon()
    app = _bare_app(prefs_path=tmp_path / "launcher.json",
                    prefs={"close_to_tray_notified": False,
                           "toggle_hotkey": launcher_main.DEFAULT_TOGGLE_HOTKEY},
                    icon=icon)
    app._on_close_to_tray()
    assert len(icon.notified) == 1
    assert "仍在后台运行" in icon.notified[0][1]
    assert json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))[
        "close_to_tray_notified"] is True
    app._on_close_to_tray()
    assert len(icon.notified) == 1  # 第二次关窗口不再弹


def test_menu_quit_stops_icon_and_shuts_down():
    icon = _FakeIcon()
    sup = _FakeSupervisor()
    app = _bare_app(icon=icon, supervisor=sup)
    app._menu_quit()
    assert app._exit.is_set()
    assert icon.stopped is True
    assert sup.shutdown_called is True


# --------------------------------------------------------------------------- #
# 打开主界面（热键 / 托盘菜单）：只用 Edge --app 窗口
# --------------------------------------------------------------------------- #
def test_toggle_main_window_opens_ui(monkeypatch):
    app = _bare_app(supervisor=_FakeSupervisor())
    opened = []
    monkeypatch.setattr(win, "open_app_window", lambda url: opened.append(url))
    app._toggle_main_window()
    assert opened == ["http://127.0.0.1:8000/ui"]


def test_open_ui_opens_browser(monkeypatch):
    app = _bare_app(supervisor=_FakeSupervisor())
    opened = []
    monkeypatch.setattr(win, "open_app_window", lambda url: opened.append(url))
    app._menu_open_ui()
    assert opened == ["http://127.0.0.1:8000/ui"]


def test_open_settings_opens_browser_with_deep_link(monkeypatch):
    app = _bare_app(supervisor=_FakeSupervisor())
    opened = []
    monkeypatch.setattr(win, "open_app_window", lambda url: opened.append(url))
    app._menu_open_settings()
    assert opened == ["http://127.0.0.1:8000/ui#sys/settings"]


# --------------------------------------------------------------------------- #
# 托盘里的登录信息：登录状态 / 登录人 / 使用权限
# --------------------------------------------------------------------------- #
def test_tray_login_items_are_read_only_and_come_first(tmp_path):
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


# --------------------------------------------------------------------------- #
# 托盘里的报警：摘要文字、新报警气泡、开关
# （自 1.0.8 平行包移植；_menu_open_alarms 在 1.0.12 走 Edge --app 窗口，不走桌面窗口）
# --------------------------------------------------------------------------- #
def _alarms(active=0, critical=0, max_id=0, latest=()):
    return {"active": active, "critical": critical, "max_id": max_id, "latest": list(latest)}


def _a(i, msg="m", sev="warning"):
    return {"id": i, "severity": sev, "message": msg}


def test_alarm_menu_text_variants():
    app = _bare_app(supervisor=_FakeSupervisor())
    assert app._alarm_text() == "当前报警：未获取"
    app._alarms = _alarms(0)
    assert app._alarm_text() == "当前报警：无未处理"
    app._alarms = _alarms(3, 0)
    assert app._alarm_text() == "当前报警：3 条未处理"
    app._alarms = _alarms(3, 1)
    assert app._alarm_text() == "当前报警：3 条未处理（严重 1）"


def test_alarm_title_shows_count_only_when_there_are_alarms():
    app = _bare_app(supervisor=_FakeSupervisor())
    app._ui_state = {"logged_in": True, "username": "ops"}
    assert app._tray_title().endswith("已登录：ops")
    app._alarms = _alarms(2, 1)
    assert app._tray_title().endswith("已登录：ops · 报警 2")


def _capture_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(win, "notify", lambda icon, title, text: sent.append((title, text)))
    return sent


def test_first_poll_records_baseline_and_does_not_pop_old_alarms(monkeypatch):
    sent = _capture_notify(monkeypatch)
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True})
    assert app._handle_alarms(_alarms(5, 1, max_id=40, latest=[_a(40), _a(39)])) is True
    assert sent == [] and app._alarm_baseline == 40


def test_new_alarm_pops_one_balloon_and_critical_is_labelled(monkeypatch):
    sent = _capture_notify(monkeypatch)
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True})
    app._handle_alarms(_alarms(1, 0, 10, [_a(10)]))
    app._handle_alarms(_alarms(2, 1, 12, [_a(12, "反应器温度过高", "critical"), _a(11, "压力偏低")]))
    assert sent == [("AIMS 严重报警", "反应器温度过高（另有 1 条）")]
    app._handle_alarms(_alarms(2, 1, 12, [_a(12), _a(11)]))         # 没有更新的报警：不重复弹
    assert len(sent) == 1
    app._handle_alarms(_alarms(3, 1, 13, [_a(13, "液位低"), _a(12)]))
    assert sent[-1] == ("AIMS 新报警", "液位低")


def test_alarm_balloon_respects_the_toggle_but_baseline_still_advances(monkeypatch):
    sent = _capture_notify(monkeypatch)
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True})
    app.prefs["alarm_notify"] = False
    app._handle_alarms(_alarms(0, 0, 5, []))
    app._handle_alarms(_alarms(1, 0, 6, [_a(6)]))
    assert sent == [] and app._alarm_baseline == 6
    app.prefs["alarm_notify"] = True                              # 重新打开后只弹之后的新报警，不补弹
    app._handle_alarms(_alarms(1, 0, 6, [_a(6)]))
    assert sent == []


def test_resolved_before_poll_means_no_balloon(monkeypatch):
    sent = _capture_notify(monkeypatch)
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True})
    app._handle_alarms(_alarms(0, 0, 5, []))
    app._handle_alarms(_alarms(0, 0, 7, []))                      # 7 号已被处理掉，不在“未处理”里
    assert sent == [] and app._alarm_baseline == 7


def test_toggle_menu_flips_and_saves_pref(monkeypatch, tmp_path):
    import json
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True},
                    prefs_path=tmp_path / "prefs.json")
    app._menu_toggle_alarm_notify()
    assert app.prefs["alarm_notify"] is False
    assert json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))["alarm_notify"] is False
    app._menu_toggle_alarm_notify()
    assert app.prefs["alarm_notify"] is True


def test_alarm_menu_opens_alarm_page_via_deep_link(monkeypatch):
    opened = []
    monkeypatch.setattr(win, "open_app_window", lambda url: opened.append(url))
    app = _bare_app(supervisor=_FakeSupervisor())
    app._menu_open_alarms()
    assert opened == ["http://127.0.0.1:8000/ui#alarm"]


def test_prefs_default_and_validation(tmp_path):
    from app.launcher.main import load_launcher_prefs
    assert load_launcher_prefs(tmp_path / "none.json")["alarm_notify"] is True
    (tmp_path / "p.json").write_text('{"alarm_notify": false}', encoding="utf-8")
    assert load_launcher_prefs(tmp_path / "p.json")["alarm_notify"] is False
    (tmp_path / "p.json").write_text('{"alarm_notify": "no"}', encoding="utf-8")
    assert load_launcher_prefs(tmp_path / "p.json")["alarm_notify"] is True      # 非布尔值 → 默认


def test_refresh_splits_alarms_out_of_login_state_and_clears_when_backend_is_gone(monkeypatch):
    app = _bare_app(supervisor=_FakeSupervisor(), prefs={"alarm_notify": True})
    seen = []
    monkeypatch.setattr(app, "_fetch_ui_state", lambda with_alarms=False: (
        seen.append(with_alarms) or {"logged_in": True, "username": "ops", "alarms": _alarms(2, 1, 9, [_a(9)])}))
    app._refresh_ui_state(with_alarms=True)
    assert seen == [True] and "alarms" not in app._ui_state and app._alarms["active"] == 2
    monkeypatch.setattr(app, "_fetch_ui_state", lambda with_alarms=False: None)
    app._refresh_ui_state()
    assert app._ui_state is None and app._alarms is None             # 后端连不上：不再显示旧的报警数
