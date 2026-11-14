"""ctypes 托盘（tray_win32）：不依赖 pystray / Pillow。

非 Windows 上测三件事：
  1. 结构体大小与 Windows x64 一致（定宽类型，才能在 Linux 上校验）；
  2. 菜单模型（文字 / 勾选 / 灰显 / 默认项 / 分隔线 / & 转义）；
  3. 窗口过程的逻辑：用一个“假 Win32”记录调用，验证右键弹菜单、左键默认动作、
     改提示文字、气泡通知、资源管理器重启后补挂图标、关闭时删图标并退出消息循环。
真正调用系统接口的那层（_Win32）只能在 Windows 上跑，这里测不到。
"""
from __future__ import annotations

import ctypes
import sys

import pytest

from app.launcher import platform_win as win
from app.launcher import tray_win32 as tw
from app.launcher.tray_win32 import Menu, MenuItem, TrayIcon, build_entries

needs_64bit = pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="大小校验按 64 位")


# ------------------------------------------------------------------ 结构体
@needs_64bit
def test_struct_sizes_match_windows_x64():
    assert ctypes.sizeof(tw.POINT) == 8
    assert ctypes.sizeof(tw.MSG) == 48
    assert ctypes.sizeof(tw.WNDCLASSW) == 72
    assert ctypes.sizeof(tw.GUID) == 16
    assert ctypes.sizeof(tw.NOTIFYICONDATAW) == 976      # Shell_NotifyIconW 的 v3 结构（Vista+）


@needs_64bit
def test_notifyicondata_field_offsets():
    off = lambda f: getattr(tw.NOTIFYICONDATAW, f).offset  # noqa: E731
    assert (off("hWnd"), off("uID"), off("uFlags"), off("uCallbackMessage"), off("hIcon")) == (8, 16, 20, 24, 32)
    assert (off("szTip"), off("dwState"), off("szInfo"), off("uTimeout")) == (40, 296, 304, 816)
    assert (off("szInfoTitle"), off("dwInfoFlags"), off("guidItem"), off("hBalloonIcon")) == (820, 948, 952, 968)


def test_utf16_units_truncates_without_splitting_surrogates():
    assert tw.utf16_units("AIMS", 128) == [65, 73, 77, 83]
    assert tw.utf16_units("中文", 128) == [0x4E2D, 0x6587]
    assert len(tw.utf16_units("a" * 500, 128)) == 127                 # 留一位给结尾 0
    cut = tw.utf16_units("a" * 126 + "😀", 128)                        # 😀 = 2 个码元，放不下整个
    assert len(cut) == 126 and all(u < 0xD800 for u in cut)


def test_fill_zero_terminates_and_clears_old_text():
    nid = tw.NOTIFYICONDATAW()
    tw._fill(nid.szTip, "longer text")
    tw._fill(nid.szTip, "ab")
    assert list(nid.szTip[:4]) == [97, 98, 0, 0]


# ------------------------------------------------------------------ 菜单模型
def test_build_entries_ids_and_separator_cleanup():
    a, b, c = MenuItem("a"), MenuItem("b"), MenuItem("c")
    es = build_entries([Menu.SEPARATOR, a, Menu.SEPARATOR, Menu.SEPARATOR, b, c, Menu.SEPARATOR])
    assert [(e.id, e.item) for e in es] == [(1, a), (0, None), (2, b), (3, c)]      # 首尾 / 连续分隔线被去掉


def test_menuitem_text_checked_enabled():
    it = MenuItem(lambda item: "dyn", lambda: None, checked=lambda item: True)
    assert it.label() == "dyn" and it.is_checked() and it.runnable
    assert not MenuItem("x", None).runnable and not MenuItem("x", lambda: None, enabled=False).runnable
    boom = MenuItem(lambda item: 1 / 0, checked=lambda item: 1 / 0)
    assert boom.label() == "?" and boom.is_checked() is False         # 取值出错不能让菜单弹不出来


def test_escape_menu_text():
    assert tw.escape_menu_text("A & B") == "A && B"


# ------------------------------------------------------------------ 假 Win32 驱动窗口过程
class FakeApi:
    def __init__(self, choose=0):
        self.calls = []
        self.choose = choose
        self.menu = []
        self.nid_log = []
        self.WNDPROC = lambda fn: fn

    def __getattr__(self, name):                      # 没特别实现的系统函数：记录一下，返回 1
        def f(*a):
            self.calls.append((name, a))
            return 1
        return f

    def CreatePopupMenu(self):
        self.menu = []
        return 99

    def AppendMenuW(self, h, flags, ident, text):
        self.menu.append((flags, ident, text))
        return 1

    def TrackPopupMenu(self, *a):
        self.calls.append(("TrackPopupMenu", a))
        return self.choose

    def Shell_NotifyIconW(self, code, ref):
        nid = ref._obj
        self.nid_log.append((code, nid.uFlags, bytes(nid.szTip), bytes(nid.szInfo), bytes(nid.szInfoTitle), nid.dwInfoFlags))
        return 1

    def names(self):
        return [c[0] for c in self.calls]


def _tray(api, items):
    t = TrayIcon("aims", "AIMS 设备智能运维系统", None, api_factory=lambda: api)
    t._api, t._hwnd, t._hicon = api, 1234, 5
    t.menu = list(items)
    t._run_async = lambda fn: fn()                     # 动作同步执行，便于断言
    return t


def test_right_click_builds_menu_and_runs_chosen_action():
    ran = []
    items = [MenuItem(lambda i: "登录状态：已登录", None, enabled=False),
             Menu.SEPARATOR,
             MenuItem("打开 & 主界面", lambda: ran.append("open"), default=True),
             MenuItem("开机自动启动", lambda: ran.append("auto"), checked=lambda i: True),
             MenuItem("退出程序", lambda: ran.append("quit"))]
    api = FakeApi(choose=3)                            # 命令号 3 = 第 3 个菜单项（分隔线不占号）
    t = _tray(api, items)
    assert t._on_message(1234, tw.WM_TRAY, 0, tw.WM_RBUTTONUP) == 0
    assert api.menu == [
        (tw.MF_STRING | tw.MF_GRAYED, 1, "登录状态：已登录"),
        (tw.MF_SEPARATOR, 0, None),
        (tw.MF_STRING, 2, "打开 && 主界面"),
        (tw.MF_STRING | tw.MF_CHECKED, 3, "开机自动启动"),
        (tw.MF_STRING, 4, "退出程序"),
    ]
    assert ("SetMenuDefaultItem", (99, 2, 0)) in api.calls          # 默认项加粗
    assert ran == ["auto"]
    assert "DestroyMenu" in api.names() and "SetForegroundWindow" in api.names()


def test_menu_cancelled_runs_nothing_and_disabled_item_is_inert():
    ran = []
    api = FakeApi(choose=0)
    t = _tray(api, [MenuItem("a", lambda: ran.append("a"))])
    t._on_message(1234, tw.WM_TRAY, 0, tw.WM_RBUTTONUP)
    assert ran == []
    t.menu = [MenuItem("off", lambda: ran.append("off"), enabled=False)]
    api.choose = 1
    t._on_message(1234, tw.WM_TRAY, 0, tw.WM_RBUTTONUP)
    assert ran == []


def test_left_click_runs_default_item_only():
    ran = []
    t = _tray(FakeApi(), [MenuItem("x", lambda: ran.append("x")), MenuItem("open", lambda: ran.append("open"), default=True)])
    t._on_message(1234, tw.WM_TRAY, 0, tw.WM_LBUTTONUP)
    assert ran == ["open"]


def test_action_exception_is_logged_not_raised(caplog):
    t = _tray(FakeApi(choose=1), [MenuItem("bad", lambda: 1 / 0)])
    t._on_message(1234, tw.WM_TRAY, 0, tw.WM_RBUTTONUP)             # 不抛
    assert "tray action failed" in caplog.text


def test_title_and_notice_are_sent_from_the_tray_thread():
    api = FakeApi()
    t = _tray(api, [])
    t.title = "AIMS — 已登录：ops"                  # 任意线程只“戳”一下窗口
    t.notify("后台运行中", "AIMS")
    assert [n for n in api.names() if n == "PostMessageW"] == ["PostMessageW", "PostMessageW"]
    t._on_message(1234, tw.WM_REFRESH, 0, 0)        # 托盘线程收到后才真正改
    modify, info = api.nid_log[-2], api.nid_log[-1]
    assert modify[0] == tw.NIM_MODIFY and modify[1] & tw.NIF_TIP
    assert "已登录：ops".encode("utf-16-le") in modify[2]
    assert info[0] == tw.NIM_MODIFY and info[1] == tw.NIF_INFO and info[5] == tw.NIIF_INFO
    assert "后台运行中".encode("utf-16-le") in info[3] and "AIMS".encode("utf-16-le") in info[4]
    assert t._pending_notice is None                # 发过就清掉，不重复弹


def test_icon_is_readded_after_explorer_restarts():
    api = FakeApi()
    t = _tray(api, [])
    t._taskbar_created = 0xC123
    t._on_message(1234, 0xC123, 0, 0)
    assert api.nid_log[-1][0] == tw.NIM_ADD


def test_close_deletes_icon_and_quits_loop():
    api = FakeApi()
    t = _tray(api, [])
    t._on_message(1234, tw.WM_CLOSE, 0, 0)
    assert "DestroyWindow" in api.names()
    t._on_message(1234, tw.WM_DESTROY, 0, 0)
    assert api.nid_log[-1][0] == tw.NIM_DELETE and ("PostQuitMessage", (0,)) in api.calls


def test_unknown_message_goes_to_default_window_proc():
    api = FakeApi()
    t = _tray(api, [])
    t._on_message(1234, 0x0001, 7, 8)
    assert api.calls[-1] == ("DefWindowProcW", (1234, 0x0001, 7, 8))


def test_stop_posts_close_and_is_safe_before_start():
    api = FakeApi()
    t = _tray(api, [])
    t.stop()
    assert ("PostMessageW", (1234, tw.WM_CLOSE, 0, 0)) in api.calls
    early = TrayIcon("aims", "t", None, api_factory=lambda: api)
    early.stop()                                    # 窗口还没建：只记旗，不崩
    assert early._stop_requested is True


# ------------------------------------------------------------------ 平台层接口
@pytest.mark.skipif(sys.platform == "win32", reason="非 Windows 才有“没有托盘”")
def test_no_tray_off_windows():
    assert win.tray_available() is False
    assert win.build_tray_icon(None) is None
    assert win.notify(None, "t", "x") is None


def test_launcher_does_not_import_pystray_or_pillow():
    """装不装 pystray / Pillow 都不影响：启动器代码里不能再有对它们的 import（注释不算）。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "launcher"
    banned = {"pystray", "PIL"}
    for f in root.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert m.split(".")[0] not in banned, f"{f.name} 仍然 import {m}"


def test_platform_layer_uses_tray_win32_when_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    ico = tmp_path / "aims.ico"
    icon = win.build_tray_icon(ico)
    assert isinstance(icon, TrayIcon) and icon.icon_path == ico and icon.name == "aims"
    started = []
    monkeypatch.setattr(TrayIcon, "run_detached", lambda self: started.append(len(self.menu)))
    assert win.run_tray_detached(icon, [MenuItem("a"), MenuItem("b")]) is True and started == [2]
    monkeypatch.setattr(TrayIcon, "run_detached", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert win.run_tray_detached(icon, []) is False                # 起不来 → 回退主线程托盘
