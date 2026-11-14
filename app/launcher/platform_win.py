"""Windows 平台相关的薄封装：单实例互斥、消息框、托盘、开机启动、应用窗口。

这里的每个函数在非 Windows 上都退化为安全的空实现，因此本模块可以被
Linux/macOS 上的测试直接导入 —— 启动器的业务流程（supervisor）因此可以
在没有 Windows 的机器上做真实的启动/停止/健康检查测试。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger("aims.launcher.win")

IS_WINDOWS = os.name == "nt"
MUTEX_NAME = "Global\\AIMS.Desktop.Launcher.7f3c1a2b"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AIMS"


# --------------------------------------------------------------------------- #
# 单实例
# --------------------------------------------------------------------------- #
class SingleInstance:
    """命名互斥体实现的单实例锁（Windows）／文件锁（其它平台）。

    用户双击两次桌面图标时，第二次启动必须立刻退出并把已有窗口叫到前台，
    而不是再起一套后端（那会撞端口，并有两条写同一个数据库的连接）。
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle = None
        self._lock_file = None
        self.already_running = False

    def acquire(self) -> bool:
        if IS_WINDOWS:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, self.name)
            last = kernel32.GetLastError()
            self._handle = handle
            # ERROR_ALREADY_EXISTS
            self.already_running = last == 183
            return not self.already_running
        import fcntl

        lock = Path(os.environ.get("AIMS_DATA_DIR") or Path.home() / ".aims") / "launcher.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(lock, "w+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()
            return True
        except OSError:
            self.already_running = True
            return False

    def release(self) -> None:
        if IS_WINDOWS and self._handle:
            try:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
        elif self._lock_file:
            try:
                self._lock_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._lock_file = None


# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
def message_box(title: str, text: str, *, error: bool = False) -> None:
    logger.info("message box: %s — %s", title, text.splitlines()[0] if text else "")
    if IS_WINDOWS:
        try:
            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"[{title}] {text}", file=sys.stderr if error else sys.stdout)


def ask_yes_no(title: str, text: str) -> bool:
    if IS_WINDOWS:
        try:
            # MB_YESNO | MB_ICONQUESTION，IDYES == 6
            return ctypes.windll.user32.MessageBoxW(None, text, title, 0x04 | 0x20) == 6
        except Exception:  # noqa: BLE001
            pass
    return False


def set_dpi_aware() -> None:
    """避免 125%/150% 缩放下托盘菜单和消息框发虚。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 主界面窗口
# --------------------------------------------------------------------------- #
def _find_edge() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def open_app_window_proc(url: str) -> tuple[str, subprocess.Popen | None]:
    """打开主界面，返回 (方式, 浏览器进程)。

    找不到 Chromium 内核浏览器时退回系统默认浏览器（此时没有可跟踪的进程，
    返回 None）。调用方想在窗口关闭时得到通知（例如托盘“最小化到托盘”），
    可以等这个进程退出；注意：用同一个 --user-data-dir 再开窗口时，新进程
    会立刻把请求交给已在跑的浏览器进程后退出，所以“进程退出”只代表
    “这个 profile 的最后一个窗口关了”，且启动后几秒内就退出的一律按
    “交接”处理，不要当成用户关窗口。
    """
    exe = _find_edge() if IS_WINDOWS else None
    if exe is not None:
        profile = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AIMS" / "ui-profile"
        try:
            profile.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [str(exe), f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check", "--window-size=1440,900"],
                close_fds=True,
                # CREATE_NO_WINDOW 兜底：浏览器本体是 GUI 程序不会弹控制台，
                # 但加上无副作用，可防被替换成控制台版启动器时报黑框。
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return f"app-window:{exe.name}", proc
        except Exception as exc:  # noqa: BLE001
            logger.warning("app window failed (%s), falling back to default browser", exc)
    import webbrowser

    try:
        webbrowser.open(url)
        return "default-browser", None
    except Exception as exc:  # noqa: BLE001
        logger.error("could not open UI: %s", exc)
        return "failed", None


def open_app_window(url: str) -> str:
    """打开主界面，优先用 Edge/Chrome 的 --app 模式（独立窗口、无地址栏、像桌面软件）。

    找不到 Chromium 内核浏览器时退回系统默认浏览器 —— 功能完全一样，只是带地址栏。
    返回实际使用的方式，便于日志和诊断。
    """
    how, _proc = open_app_window_proc(url)
    return how


# --------------------------------------------------------------------------- #
# 开机启动（当前用户，HKCU，无需管理员）
# --------------------------------------------------------------------------- #
def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                exe = sys.executable if getattr(sys, "frozen", False) else str(Path(sys.argv[0]).resolve())
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" --minimized')
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        logger.warning("autostart toggle failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
def tray_available() -> bool:
    """托盘只靠系统自带的 Win32 接口（ctypes），不需要 pystray / Pillow；非 Windows 没有托盘。"""
    return IS_WINDOWS


def build_tray_icon(icon_path: Path | None):
    """构造托盘图标对象（见 tray_win32.TrayIcon）；非 Windows 或创建失败返回 None（启动器无托盘继续运行）。

    .ico 缺失 / 读不了时托盘会用系统默认应用图标，不会因为缺图标起不来。
    """
    if not tray_available():
        return None
    try:
        from app.launcher.tray_win32 import TrayIcon

        return TrayIcon("aims", "AIMS 设备智能运维系统", icon_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tray icon unavailable: %s", exc)
        return None


def run_tray(icon, items: Iterable) -> None:  # pragma: no cover - GUI 循环
    icon.menu = list(items)
    icon.run()


def notify(icon, title: str, text: str) -> None:
    try:
        if icon is not None:
            icon.notify(text, title)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 桌面/开始菜单快捷方式（安装程序已经建好，这里是"修复"用）
# --------------------------------------------------------------------------- #
def create_shortcut(path: Path, target: Path, *, args: str = "", workdir: Path | None = None,
                    icon: Path | None = None, description: str = "") -> bool:
    if not IS_WINDOWS:
        return False
    script = (
        "$W = New-Object -ComObject WScript.Shell;"
        f"$S = $W.CreateShortcut('{path}');"
        f"$S.TargetPath = '{target}';"
        f"$S.Arguments = '{args}';"
        f"$S.WorkingDirectory = '{workdir or target.parent}';"
        f"$S.IconLocation = '{icon or target}';"
        f"$S.Description = '{description}';"
        "$S.Save()"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60,
                             # powershell.exe 是控制台程序：GUI 启动器里调用必须
                             # 压掉窗口，否则创建快捷方式时闪出蓝色 PowerShell 框。
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if res.returncode != 0:
            logger.warning("shortcut failed: %s", res.stderr.strip()[:300])
        return res.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("shortcut failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# AIMS Desktop 原生窗口（pywebview / Edge WebView2）
# --------------------------------------------------------------------------- #
def webview_available() -> bool:
    """pywebview 是否可用（Windows 上经由 Edge WebView2 渲染）。

    WebView2 Runtime 在 Windows 11 随系统自带；缺失时这里返回 False，
    启动器自动回退到 Edge --app 窗口 / 系统浏览器，功能完全等价。
    """
    try:
        import webview  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def run_tray_detached(icon, items: Iterable) -> bool:
    """在后台线程里跑托盘图标（供主线程跑 GUI 消息循环的窗口模式用）。

    起不来（窗口类注册失败、Shell_NotifyIconW 失败等）返回 False，调用方回退到
    "托盘在主线程" 的传统模式。
    """
    try:
        icon.menu = list(items)
        icon.run_detached()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("detached tray failed: %s", exc)
        return False


def run_desktop_window(url: str, *, title: str = "AIMS", width: int = 1280,
                       height: int = 800, on_close_to_tray=None,
                       on_window=None, is_quitting=None) -> None:
    """以原生桌面窗口打开主界面，阻塞到窗口被真正销毁。

    与 run_standalone._open_ui_window 相同的注意点：不设 on_top=True
    （pywebview 6.2.1 + EdgeChromium 在激活压力下会死锁，见 r0x0r/pywebview#1823）。

    on_close_to_tray: 传了它，点 × 不会关掉窗口，而是隐藏窗口并调这个回调
    （托盘继续跑，应用不退出）；回调里通常发一次托盘通知。不传则点 ×
    直接关窗口（老行为）。on_window: 窗口建好后调一次，传窗口对象，
    调用方留着它以后 show()/hide()/load_url()。is_quitting: 退出程序时
    销毁窗口也可能触发 closing 事件，传一个“正在退出”的判断，正在退出
    就放行（不拦截、不隐藏），否则退出会被自己的拦截挡住。
    """
    import webview

    try:  # pywebview 5+：允许页面里的 <a download>（CSV/Excel 导出）弹出保存对话框
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:  # noqa: BLE001
        pass
    window = webview.create_window(title, url, width=width, height=height,
                                   resizable=True, text_select=True)
    if window is not None and on_close_to_tray is not None:
        def _on_closing():
            if is_quitting is not None and is_quitting():
                return None  # 正在退出程序：放行，让窗口真正关掉
            # pywebview：closing 处理返回 False 就取消关闭。窗口只是藏起来，
            # 事件循环继续跑，托盘菜单 / 快捷键随时能再把它叫出来。
            try:
                window.hide()
            except Exception:  # noqa: BLE001
                pass
            try:
                on_close_to_tray()
            except Exception:  # noqa: BLE001
                logger.exception("close-to-tray callback failed")
            return False

        try:
            window.events.closing += _on_closing
        except Exception:  # noqa: BLE001
            logger.warning("could not hook window closing event; close will quit")
    if window is not None and on_window is not None:
        try:
            on_window(window)
        except Exception:  # noqa: BLE001
            pass
    webview.start()


def show_desktop_window(window) -> bool:
    """把藏起来的原生窗口再显示出来（托盘菜单 / 快捷键用）。"""
    if window is None:
        return False
    try:
        window.show()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("show desktop window failed")
        return False


def hide_desktop_window(window) -> bool:
    """把原生窗口藏到托盘（快捷键切换用）。"""
    if window is None:
        return False
    try:
        window.hide()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("hide desktop window failed")
        return False


def destroy_desktop_window(window) -> bool:
    """真正销毁原生窗口（退出程序时用；会让 webview.start() 返回）。"""
    if window is None:
        return False
    try:
        window.destroy()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("destroy desktop window failed")
        return False


def load_desktop_url(window, url: str) -> bool:
    """让已有的原生窗口跳到指定地址（例如托盘“设置”直接打开设置页）。"""
    if window is None:
        return False
    try:
        window.load_url(url)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("load desktop url failed")
        return False


# --------------------------------------------------------------------------- #
# 全局快捷键（Windows：RegisterHotKey；其它平台不支持，调用方直接跳过）
# --------------------------------------------------------------------------- #
# 修饰键（winuser.h MOD_*）与虚拟键码：只认“修饰键 + 一个键”的组合，
# 单个字母/数字不接受（否则正常打字会被劫持）。
_MOD_MAP = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
_FKEY_VK = {f"f{i}": 0x70 + (i - 1) for i in range(1, 25)}
_WM_HOTKEY = 0x0312


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """解析 "ctrl+alt+a" 这样的快捷键，返回 (修饰键位掩码, 虚拟键码)。

    接受：ctrl/alt/shift/win（任意组合，至少一个）+ 一个键（a-z、0-9、f1-f24）。
    其它写法一律返回 None（调用方提示用户重填）。
    """
    if not text:
        return None
    parts = [p.strip().lower() for p in text.split("+")]
    if len(parts) < 2 or any(not p for p in parts):
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in _MOD_MAP:
            return None
        mods |= _MOD_MAP[p]
    if mods == 0:
        return None
    key = parts[-1]
    if len(key) == 1 and key.isalnum() and key.isascii():
        return mods, ord(key.upper())
    if key in _FKEY_VK:
        return mods, _FKEY_VK[key]
    return None


def format_hotkey(mods: int, vk: int) -> str:
    """parse_hotkey 的逆操作：把 (修饰键, 虚拟键) 写回 "ctrl+alt+a" 形式。"""
    names = []
    for name, bit in (("ctrl", 0x0002), ("alt", 0x0001), ("shift", 0x0004), ("win", 0x0008)):
        if mods & bit:
            names.append(name)
    if 0x41 <= vk <= 0x5A:
        names.append(chr(vk).lower())
    elif 0x30 <= vk <= 0x39:
        names.append(chr(vk))
    elif 0x70 <= vk <= 0x87:
        names.append(f"f{vk - 0x70 + 1}")
    else:
        names.append(f"vk{vk}")
    return "+".join(names)


class HotkeyListener:
    """在后台线程里等一个 Windows 全局快捷键，按下就调 callback。

    用消息窗口（HWND_MESSAGE）收 WM_HOTKEY：不需要可见窗口，不抢焦点。
    非 Windows 上 start() 直接返回 False，调用方按“不支持”处理。
    """

    def __init__(self, hotkey: tuple[int, int] | None, callback: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self._thread = None
        self._hwnd = None
        self._running = False

    def start(self) -> bool:
        if not IS_WINDOWS or not self.hotkey:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        import threading

        self._running = True
        self._thread = threading.Thread(target=self._loop, name="aims-hotkey",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            # 等线程退干净（它会先 UnregisterHotKey）：换新热键时立即重注册才不会撞上旧的。
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        try:
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            mods, vk = self.hotkey
            # 消息窗口：只收消息，不显示。
            wc = wt.WNDCLASSW()
            wc.lpfnWndProc = wt.WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
            wc.lpszClassName = "AIMS.HotkeyListener"
            wc.hInstance = kernel32.GetModuleHandleW(None)
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "AIMS Hotkey", 0,
                                          0, 0, 0, 0, -3, None, wc.hInstance, None)  # HWND_MESSAGE = -3
            if not hwnd:
                logger.warning("hotkey listener: no message window")
                return
            self._hwnd = hwnd
            if not user32.RegisterHotKey(hwnd, 1, mods, vk):
                logger.warning("hotkey listener: RegisterHotKey failed (maybe taken)")
                user32.DestroyWindow(hwnd)
                return
            msg = wt.MSG()
            while self._running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == _WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001
                        logger.exception("hotkey callback failed")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            logger.exception("hotkey listener failed")
        finally:
            try:
                if self._hwnd:
                    user32 = ctypes.windll.user32
                    user32.UnregisterHotKey(self._hwnd, 1)
                    user32.DestroyWindow(self._hwnd)
            except Exception:  # noqa: BLE001
                pass
            self._hwnd = None
