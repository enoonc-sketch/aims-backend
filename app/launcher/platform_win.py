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


def open_app_window(url: str) -> str:
    """打开主界面，优先用 Edge/Chrome 的 --app 模式（独立窗口、无地址栏、像桌面软件）。

    找不到 Chromium 内核浏览器时退回系统默认浏览器 —— 功能完全一样，只是带地址栏。
    返回实际使用的方式，便于日志和诊断。
    """
    exe = _find_edge() if IS_WINDOWS else None
    if exe is not None:
        profile = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AIMS" / "ui-profile"
        try:
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [str(exe), f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check", "--window-size=1440,900"],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            return f"app-window:{exe.name}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("app window failed (%s), falling back to default browser", exc)
    import webbrowser

    try:
        webbrowser.open(url)
        return "default-browser"
    except Exception as exc:  # noqa: BLE001
        logger.error("could not open UI: %s", exc)
        return "failed"


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
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def build_tray_icon(icon_path: Path | None):
    """构造 pystray 图标；没有 pystray/Pillow 时返回 None（启动器继续无托盘运行）。"""
    if not tray_available():
        return None
    import pystray
    from PIL import Image, ImageDraw

    if icon_path and icon_path.is_file():
        image = Image.open(icon_path)
        image.load()
    else:  # 兜底：画一个简单的圆角方块，避免因缺图标而无法启动
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(23, 92, 211, 255))
        draw.rectangle((18, 30, 46, 36), fill=(255, 255, 255, 255))
        draw.rectangle((30, 18, 36, 48), fill=(255, 255, 255, 255))
    return pystray.Icon("aims", image, "AIMS 设备智能运维系统")


def run_tray(icon, items: Iterable) -> None:  # pragma: no cover - GUI 循环
    import pystray

    icon.menu = pystray.Menu(*items)
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
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            logger.warning("shortcut failed: %s", res.stderr.strip()[:300])
        return res.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("shortcut failed: %s", exc)
        return False
