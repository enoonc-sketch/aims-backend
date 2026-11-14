"""Windows 托盘：只用 Python 标准库 ctypes 调系统接口，不依赖 pystray / Pillow。

画图标用 Shell_NotifyIconW，右键菜单用 CreatePopupMenu / TrackPopupMenu，
图标直接从 .ico 文件加载（LoadImageW）。装不装 pystray / Pillow 都不影响。

对外接口刻意与 pystray 的用法对齐，启动器（main.py）几乎不用改：

    TrayIcon(name, title, icon_path)
      .title            可写：改托盘提示文字
      .menu             菜单项列表（MenuItem / Menu.SEPARATOR）
      .run()            在当前线程跑消息循环，直到 stop()
      .run_detached()   在后台线程跑；起不来抛异常
      .notify(text, title)   气泡通知
      .update_menu()    菜单每次右键弹出时现建，这里无事可做（保留是为了接口一致）
      .stop()           任意线程可调

    MenuItem(text, action=None, default=False, checked=None, enabled=True)
      text     字符串，或一个接收 item 的函数（每次弹出菜单现取）
      action   无参函数；在独立线程里执行，慢动作（备份、重启服务）不会卡住托盘
      default  左键单击托盘图标时执行，菜单里加粗
      checked  返回 bool 的函数，菜单里打勾
      enabled  False 时灰显、点不了

结构体全部用定宽类型（c_uint32 / c_void_p / c_uint16 数组），所以结构体大小在任何平台上
都和 Windows x64 一致，可以在非 Windows 上做大小校验；真正调用系统接口的部分
（_Win32）只在 Windows 上才会加载。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional, Union

logger = logging.getLogger("aims.launcher.tray")

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# 菜单模型（纯 Python，任何平台都能测）
# --------------------------------------------------------------------------- #
class _Separator:
    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "SEPARATOR"


class Menu:
    SEPARATOR = _Separator()


class MenuItem:
    def __init__(self, text: Union[str, Callable], action: Optional[Callable[[], None]] = None,
                 default: bool = False, checked: Optional[Callable] = None, enabled: bool = True) -> None:
        self.text = text
        self.action = action
        self.default = default
        self.checked = checked
        self.enabled = enabled

    def label(self) -> str:
        try:
            return str(self.text(self) if callable(self.text) else self.text)
        except Exception:  # noqa: BLE001 - 文字取不到不能让整个菜单弹不出来
            logger.exception("tray menu label failed")
            return "?"

    def is_checked(self) -> bool:
        if self.checked is None:
            return False
        try:
            return bool(self.checked(self))
        except Exception:  # noqa: BLE001
            logger.exception("tray menu checked() failed")
            return False

    @property
    def runnable(self) -> bool:
        return bool(self.enabled and self.action is not None)


class Entry(NamedTuple):
    id: int                      # 菜单命令号（从 1 起）；分隔线为 0
    item: Optional[MenuItem]     # None = 分隔线


def build_entries(items: Iterable) -> list[Entry]:
    """把菜单项展开成带命令号的条目；开头 / 结尾 / 连续的分隔线会被去掉。"""
    out: list[Entry] = []
    next_id = 1
    for it in items:
        if it is Menu.SEPARATOR:
            if out and out[-1].item is not None:
                out.append(Entry(0, None))
            continue
        out.append(Entry(next_id, it))
        next_id += 1
    while out and out[-1].item is None:
        out.pop()
    return out


def escape_menu_text(text: str) -> str:
    """菜单文字里的 & 会被当成快捷键下划线，要写成 &&。"""
    return text.replace("&", "&&")


# --------------------------------------------------------------------------- #
# 定宽结构体（大小与 Windows x64 一致）
# --------------------------------------------------------------------------- #
_U16 = ctypes.c_uint16
_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32
_PTR = ctypes.c_void_p


class POINT(ctypes.Structure):
    _fields_ = [("x", _I32), ("y", _I32)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", _PTR), ("message", _U32), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", _U32), ("pt", POINT)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", _U32), ("lpfnWndProc", _PTR), ("cbClsExtra", _I32), ("cbWndExtra", _I32),
                ("hInstance", _PTR), ("hIcon", _PTR), ("hCursor", _PTR), ("hbrBackground", _PTR),
                ("lpszMenuName", _PTR), ("lpszClassName", ctypes.c_wchar_p)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", _U32), ("Data2", _U16), ("Data3", _U16), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    # 文字缓冲区用 UTF-16 码元数组（不用 c_wchar：它在 Linux 上是 4 字节）。
    _fields_ = [("cbSize", _U32), ("hWnd", _PTR), ("uID", _U32), ("uFlags", _U32),
                ("uCallbackMessage", _U32), ("hIcon", _PTR), ("szTip", _U16 * 128),
                ("dwState", _U32), ("dwStateMask", _U32), ("szInfo", _U16 * 256),
                ("uTimeout", _U32), ("szInfoTitle", _U16 * 64), ("dwInfoFlags", _U32),
                ("guidItem", GUID), ("hBalloonIcon", _PTR)]


def utf16_units(text: str, capacity: int) -> list[int]:
    """文字转 UTF-16 码元，最多 capacity-1 个（留给结尾的 0）；不会把代理对从中间截断。"""
    raw = text.encode("utf-16-le", "replace")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    units = units[:capacity - 1]
    if units and 0xD800 <= units[-1] <= 0xDBFF:      # 末尾是半个代理对：丢掉
        units.pop()
    return units


def _fill(buf, text: str) -> None:
    units = utf16_units(text, len(buf))
    for i in range(len(buf)):
        buf[i] = units[i] if i < len(units) else 0


# --------------------------------------------------------------------------- #
# Win32 常量
# --------------------------------------------------------------------------- #
WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1          # 托盘图标的回调消息
WM_REFRESH = WM_APP + 2       # 其它线程要求“刷新提示文字 / 弹气泡”

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1

MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN, TPM_RETURNCMD = 0x2, 0x20, 0x100

IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
IDI_APPLICATION = 32512
SM_CXSMICON, SM_CYSMICON = 49, 50
ERROR_CLASS_ALREADY_EXISTS = 1410

CLASS_NAME = "AIMS.Launcher.TrayWindow"


class _Win32:
    """user32 / shell32 / kernel32 的函数声明。只在 Windows 上实例化。

    每个函数都显式声明 argtypes / restype：64 位下句柄和指针默认按 32 位 int 传会被截断。
    """

    def __init__(self) -> None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)      # type: ignore[attr-defined]
        s32 = ctypes.WinDLL("shell32", use_last_error=True)     # type: ignore[attr-defined]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)    # type: ignore[attr-defined]
        lresult, wparam, lparam = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t
        c_int, wstr = ctypes.c_int, ctypes.c_wchar_p
        self.WNDPROC = ctypes.WINFUNCTYPE(lresult, _PTR, _U32, wparam, lparam)  # type: ignore[attr-defined]

        def sig(fn, argtypes, restype):
            fn.argtypes, fn.restype = argtypes, restype
            return fn

        self.DefWindowProcW = sig(u32.DefWindowProcW, [_PTR, _U32, wparam, lparam], lresult)
        self.RegisterClassW = sig(u32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], _U16)
        self.CreateWindowExW = sig(u32.CreateWindowExW, [_U32, wstr, wstr, _U32, c_int, c_int, c_int, c_int,
                                                         _PTR, _PTR, _PTR, _PTR], _PTR)
        self.DestroyWindow = sig(u32.DestroyWindow, [_PTR], c_int)
        self.GetMessageW = sig(u32.GetMessageW, [ctypes.POINTER(MSG), _PTR, _U32, _U32], c_int)
        self.TranslateMessage = sig(u32.TranslateMessage, [ctypes.POINTER(MSG)], c_int)
        self.DispatchMessageW = sig(u32.DispatchMessageW, [ctypes.POINTER(MSG)], lresult)
        self.PostMessageW = sig(u32.PostMessageW, [_PTR, _U32, wparam, lparam], c_int)
        self.PostQuitMessage = sig(u32.PostQuitMessage, [c_int], None)
        self.RegisterWindowMessageW = sig(u32.RegisterWindowMessageW, [wstr], _U32)
        self.LoadImageW = sig(u32.LoadImageW, [_PTR, wstr, _U32, c_int, c_int, _U32], _PTR)
        self.LoadIconW = sig(u32.LoadIconW, [_PTR, _PTR], _PTR)
        self.DestroyIcon = sig(u32.DestroyIcon, [_PTR], c_int)
        self.GetSystemMetrics = sig(u32.GetSystemMetrics, [c_int], c_int)
        self.CreatePopupMenu = sig(u32.CreatePopupMenu, [], _PTR)
        self.AppendMenuW = sig(u32.AppendMenuW, [_PTR, _U32, ctypes.c_size_t, wstr], c_int)
        self.SetMenuDefaultItem = sig(u32.SetMenuDefaultItem, [_PTR, _U32, _U32], c_int)
        self.TrackPopupMenu = sig(u32.TrackPopupMenu, [_PTR, _U32, c_int, c_int, c_int, _PTR, _PTR], c_int)
        self.DestroyMenu = sig(u32.DestroyMenu, [_PTR], c_int)
        self.GetCursorPos = sig(u32.GetCursorPos, [ctypes.POINTER(POINT)], c_int)
        self.SetForegroundWindow = sig(u32.SetForegroundWindow, [_PTR], c_int)
        self.Shell_NotifyIconW = sig(s32.Shell_NotifyIconW, [_U32, ctypes.POINTER(NOTIFYICONDATAW)], c_int)
        self.GetModuleHandleW = sig(k32.GetModuleHandleW, [wstr], _PTR)
        self.get_last_error = ctypes.get_last_error


_api_cache: Optional[_Win32] = None


def _load_api() -> _Win32:
    global _api_cache
    if not IS_WINDOWS:
        raise RuntimeError("Windows tray is only available on Windows")
    if _api_cache is None:
        _api_cache = _Win32()
    return _api_cache


# --------------------------------------------------------------------------- #
# 托盘图标
# --------------------------------------------------------------------------- #
class TrayIcon:
    def __init__(self, name: str, title: str, icon_path: Optional[Path] = None, *,
                 api_factory: Callable[[], object] = _load_api) -> None:
        self.name = name
        self._title = title
        self.icon_path = Path(icon_path) if icon_path else None
        self.menu: list = []
        self._api_factory = api_factory
        self._api = None
        self._hwnd = None
        self._hicon = None
        self._wndproc = None             # 必须一直引用着，否则回调被回收后窗口过程会崩
        self._taskbar_created = 0
        self._pending_notice: Optional[tuple[str, str]] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = False
        # 菜单动作在独立线程里跑；测试里换成同步执行
        self._run_async: Callable[[Callable[[], None]], None] = self._spawn

    # ------------------------------------------------------------ 对外接口
    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self._poke()

    def update_menu(self) -> None:
        """菜单在每次右键弹出时现建（文字 / 勾选都现取），无需主动刷新。"""

    def notify(self, text: str, title: str = "") -> None:
        with self._lock:
            self._pending_notice = (str(text), str(title))
        self._poke()

    def run(self) -> None:
        self._loop()

    def run_detached(self) -> None:
        def main() -> None:
            try:
                self._loop()
            except BaseException as exc:  # noqa: BLE001
                self._error = exc
                logger.exception("tray thread failed")
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=main, name="aims-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("tray did not start in time")
        if self._error is not None:
            raise RuntimeError(f"tray failed to start: {self._error}")

    def stop(self) -> None:
        self._stop_requested = True
        hwnd = self._hwnd
        if hwnd and self._api is not None:
            self._api.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _spawn(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="aims-tray-action", daemon=True).start()

    def _poke(self) -> None:
        hwnd = self._hwnd
        if hwnd and self._api is not None:
            self._api.PostMessageW(hwnd, WM_REFRESH, 0, 0)

    def _make_icon_handle(self):
        api = self._api
        if self.icon_path is not None and self.icon_path.is_file():
            h = api.LoadImageW(None, str(self.icon_path), IMAGE_ICON, api.GetSystemMetrics(SM_CXSMICON),
                               api.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)
            if h:
                return h, True
            logger.warning("LoadImageW failed for %s", self.icon_path)
        # 没有 / 读不了 .ico：退回系统自带的默认应用图标，托盘照样能起来
        return api.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)), False

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        _fill(nid.szTip, self._title)
        return nid

    def _notify_icon(self, code: int, nid: NOTIFYICONDATAW) -> bool:
        return bool(self._api.Shell_NotifyIconW(code, ctypes.byref(nid)))

    def _add_icon(self) -> None:
        if not self._notify_icon(NIM_ADD, self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)):
            raise RuntimeError("Shell_NotifyIconW(NIM_ADD) failed")

    def _loop(self) -> None:
        api = self._api = self._api_factory()
        self._own_icon = False
        try:
            self._create_window()
            self._hicon, self._own_icon = self._make_icon_handle()
            self._add_icon()
            if self._stop_requested:                       # 还没起来就被要求停了
                api.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            self._ready.set()
            self._flush_updates()
            msg = MSG()
            while True:
                got = api.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got <= 0:                               # 0 = WM_QUIT，-1 = 出错
                    break
                api.TranslateMessage(ctypes.byref(msg))
                api.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hicon and self._own_icon:
                api.DestroyIcon(self._hicon)
            self._hwnd = None
            self._ready.set()

    def _create_window(self) -> None:
        api = self._api
        self._taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")   # 资源管理器重启后要重新挂图标
        self._wndproc = api.WNDPROC(self._wnd_proc)
        hinst = api.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wc.hInstance = hinst
        wc.lpszClassName = CLASS_NAME
        if not api.RegisterClassW(ctypes.byref(wc)) and api.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise RuntimeError(f"RegisterClassW failed ({api.get_last_error()})")
        # 不是 message-only 窗口：只有顶层窗口才收得到 TaskbarCreated 广播。它从不显示。
        hwnd = api.CreateWindowExW(0, CLASS_NAME, "AIMS Tray", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise RuntimeError(f"CreateWindowExW failed ({api.get_last_error()})")
        self._hwnd = hwnd

    def _flush_updates(self) -> None:
        """在托盘线程里把改过的提示文字 / 待弹的气泡发给系统。"""
        self._notify_icon(NIM_MODIFY, self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP))
        with self._lock:
            notice, self._pending_notice = self._pending_notice, None
        if notice:
            nid = self._nid(NIF_INFO)
            _fill(nid.szInfo, notice[0])
            _fill(nid.szInfoTitle, notice[1])
            nid.dwInfoFlags = NIIF_INFO
            self._notify_icon(NIM_MODIFY, nid)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            return self._on_message(hwnd, msg, wparam, lparam)
        except Exception:  # noqa: BLE001 - 回调里的异常 ctypes 只会打印并吞掉，这里自己记日志
            logger.exception("tray window proc failed (msg=0x%x)", msg)
            return self._api.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_message(self, hwnd, msg, wparam, lparam):
        api = self._api
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONUP:
                self._invoke_default()
            elif event == WM_RBUTTONUP:
                self._popup(hwnd)
            return 0
        if msg == WM_REFRESH:
            self._flush_updates()
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._add_icon()
            return 0
        if msg == WM_CLOSE:
            api.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._notify_icon(NIM_DELETE, self._nid(0))
            api.PostQuitMessage(0)
            return 0
        return api.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _invoke(self, item: Optional[MenuItem]) -> None:
        if item is None or not item.runnable:
            return

        def run() -> None:
            try:
                item.action()
            except Exception:  # noqa: BLE001
                logger.exception("tray action failed: %s", item.label())

        self._run_async(run)

    def _invoke_default(self) -> None:
        for it in self.menu:
            if it is not Menu.SEPARATOR and it.default and it.runnable:
                self._invoke(it)
                return

    def _popup(self, hwnd) -> None:
        api = self._api
        entries = build_entries(self.menu)
        if not entries:
            return
        hmenu = api.CreatePopupMenu()
        by_id: dict[int, MenuItem] = {}
        default_id = 0
        try:
            for e in entries:
                if e.item is None:
                    api.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING | (0 if e.item.enabled else MF_GRAYED) | (MF_CHECKED if e.item.is_checked() else 0)
                api.AppendMenuW(hmenu, flags, e.id, escape_menu_text(e.item.label()))
                by_id[e.id] = e.item
                if e.item.default:
                    default_id = e.id
            if default_id:
                api.SetMenuDefaultItem(hmenu, default_id, 0)
            pt = POINT()
            api.GetCursorPos(ctypes.byref(pt))
            api.SetForegroundWindow(hwnd)                  # 不先置前台，菜单点别处不会自动收起
            cmd = api.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_RETURNCMD,
                                     pt.x, pt.y, 0, hwnd, None)
            api.PostMessageW(hwnd, WM_NULL, 0, 0)          # 微软文档要求的收尾，避免菜单偶发“粘住”
        finally:
            api.DestroyMenu(hmenu)
        if cmd:
            self._invoke(by_id.get(cmd))
