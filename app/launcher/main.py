"""AIMS.exe —— 桌面启动器入口。

职责：把"双击图标"变成"数据库 + AI + 后端 + 界面都在跑"，
并用托盘菜单提供日常运维入口（备份、日志、诊断、开机启动、退出）。

启动顺序严格按依赖：PostgreSQL → AI 引擎自检 → 后端 → 打开主界面。
任何一步失败都弹中文说明 + 日志路径，绝不静默退出。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from app.launcher import APP_NAME, PRODUCT_NAME
from app.launcher import platform_win as win
from app.launcher.supervisor import Supervisor, SupervisorError, _default_root

logger = logging.getLogger("aims.launcher")


# --------------------------------------------------------------------------- #
def _setup_logging(root: Path) -> Path:
    log_dir = root / "aims_data" / "logs"
    log_path = log_dir / "launcher.log"
    # main() 和 App() 都会调用：只装一次 handler，否则每行日志写两遍。
    if getattr(_setup_logging, "_done", False):
        return log_path
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        if not getattr(sys, "frozen", False):
            root_logger.addHandler(logging.StreamHandler(sys.stdout))
        _setup_logging._done = True  # type: ignore[attr-defined]
        return log_path
    except Exception:  # noqa: BLE001
        logging.basicConfig(level=logging.INFO)
        return log_path


class Splash:
    """极简启动画面（tkinter 可用时）。没有 tk 就纯日志，不影响启动。"""

    def __init__(self, title: str = PRODUCT_NAME) -> None:
        self.root = None
        self.label = None
        try:
            import tkinter as tk

            self.root = tk.Tk()
            self.root.title(title)
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            width, height = 460, 150
            x = (self.root.winfo_screenwidth() - width) // 2
            y = (self.root.winfo_screenheight() - height) // 2
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.configure(bg="#0f172a")
            tk.Label(self.root, text="AIMS 设备智能运维系统", bg="#0f172a", fg="#ffffff",
                     font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(28, 6))
            self.label = tk.Label(self.root, text="正在启动……", bg="#0f172a", fg="#93c5fd",
                                  font=("Microsoft YaHei UI", 9))
            self.label.pack()
            tk.Label(self.root, text="本窗口会在启动完成后自动关闭", bg="#0f172a", fg="#475569",
                     font=("Microsoft YaHei UI", 8)).pack(pady=(18, 0))
            self.root.update()
        except Exception:  # noqa: BLE001
            self.root = None

    def set(self, text: str) -> None:
        if self.root is None:
            return
        try:
            self.label.config(text=text.strip())
            self.root.update()
        except Exception:  # noqa: BLE001
            self.root = None

    def close(self) -> None:
        if self.root is None:
            return
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        self.root = None


# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: Path, *, open_ui: bool = True, tray: bool = True) -> None:
        self.root = root
        self.open_ui = open_ui
        self.want_tray = tray
        self.splash = Splash()
        self.icon = None
        self.supervisor = Supervisor(root, progress=self._progress)
        self._exit = threading.Event()
        self.log_path = _setup_logging(root)

    def _progress(self, message: str) -> None:
        logger.info(message.strip())
        if self.splash:
            self.splash.set(message)

    # ------------------------------------------------------------------ 启动
    def start(self) -> bool:
        try:
            self.supervisor.start_all()
        except SupervisorError as exc:
            self.splash.close()
            self._fail("AIMS 启动失败", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected startup failure")
            self.splash.close()
            self._fail("AIMS 启动失败", f"{exc}\n\n{traceback.format_exc(limit=6)}\n\n日志：{self.log_path}")
            return False
        self.splash.close()
        if self.open_ui:
            url = self.supervisor.ui_url()
            how = win.open_app_window(url)
            logger.info("UI opened (%s): %s", how, url)
        return True

    def _fail(self, title: str, message: str) -> None:
        detail = ""
        try:
            from app.deploy import runtime

            detail = runtime.diagnostic_tail(self.supervisor.layout, lines=15)
        except Exception:  # noqa: BLE001
            pass
        text = f"{message}\n\n日志文件：{self.log_path}"
        if detail:
            text += f"\n\n--- 最近日志 ---\n{detail[-1800:]}"
        win.message_box(title, text, error=True)

    # ------------------------------------------------------------------ 菜单
    def menu_items(self):
        import pystray

        Item = pystray.MenuItem
        yield Item("打开 AIMS 主界面", lambda: win.open_app_window(self.supervisor.ui_url()), default=True)
        yield Item("立即备份数据库", self._menu_backup)
        yield Item("打开数据目录", lambda: self._open_path(self.supervisor.layout.data_dir))
        yield Item("打开日志目录", lambda: self._open_path(self.supervisor.layout.logs_dir))
        yield Item("生成诊断报告", self._menu_doctor)
        yield Item("重启服务", self._menu_restart)
        yield Item("开机自动启动", self._menu_autostart, checked=lambda _i: win.autostart_enabled())
        yield pystray.Menu.SEPARATOR
        yield Item(f"版本 {_version()}", lambda: win.message_box(
            PRODUCT_NAME, f"版本：{_version()}\n安装目录：{self.root}\n数据目录：{self.supervisor.layout.data_dir}\n"
                        f"界面地址：{self.supervisor.ui_url()}\n日志：{self.log_path}"))
        yield Item("退出", self._menu_quit)

    def _open_path(self, path: Path) -> None:
        try:
            if win.IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            win.message_box("无法打开目录", f"{path}\n{exc}", error=True)

    def _menu_backup(self) -> None:
        try:
            from app.deploy import backup, conf

            pg = self.supervisor.pg
            if pg is None or not pg.is_ready():
                win.message_box("备份失败", "数据库未运行，无法备份。", error=True)
                return
            password = conf.read_secrets(self.supervisor.layout).get("DB_SUPERUSER_PASSWORD", "")
            meta = backup.create_backup(pg, password=password, reason="tray",
                                        layout=self.supervisor.layout)
            win.message_box("备份完成",
                            f"已备份到：\n{self.supervisor.layout.backups_dir / meta['file']}\n\n"
                            f"大小：{meta['size_bytes'] / 1e6:.1f} MB")
        except Exception as exc:  # noqa: BLE001
            logger.exception("tray backup failed")
            win.message_box("备份失败", str(exc), error=True)

    def _menu_doctor(self) -> None:
        try:
            from app.deploy import doctor

            result = doctor.run(root=self.root, api_port=self.supervisor.api_port)
            path = doctor.write_report(self.supervisor.layout, result)
            win.message_box("诊断报告", doctor.report_text(result) + f"\n\n已保存：{path}")
        except Exception as exc:  # noqa: BLE001
            win.message_box("诊断失败", str(exc), error=True)

    def _menu_restart(self) -> None:
        if not win.ask_yes_no("重启服务", "将中断正在使用的界面（数据不会丢失）。继续？"):
            return
        self.supervisor.stop_all(stop_db=False)
        time.sleep(1.0)
        self.supervisor = Supervisor(self.root, progress=self._progress)
        ok = self.start()
        if ok:
            win.open_app_window(self.supervisor.ui_url())
        else:
            win.message_box("重启失败", "请查看日志或生成诊断报告。", error=True)

    def _menu_autostart(self) -> None:
        ok = win.set_autostart(not win.autostart_enabled())
        if not ok:
            win.message_box("设置失败", "无法写入开机启动项（可能被安全软件拦截）。", error=True)

    def _menu_quit(self) -> None:
        self._exit.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.supervisor.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("shutdown error")
        logger.info("launcher exited")

    # ------------------------------------------------------------------ 运行
    def run(self) -> int:
        if not self.start():
            if getattr(sys, "frozen", False) and "--quiet" not in sys.argv:
                pass
            return 3
        if not self.want_tray or not win.tray_available():
            if self.want_tray:
                logger.warning("pystray/Pillow unavailable — running without tray")
            return self._run_headless()
        icon_path = self.root / "assets" / "aims.ico"
        self.icon = win.build_tray_icon(icon_path)
        if self.icon is None:
            return self._run_headless()
        win.notify(self.icon, PRODUCT_NAME, "AIMS 已在运行，可从托盘菜单打开或退出")
        try:
            win.run_tray(self.icon, list(self.menu_items()))
        finally:
            if not self._exit.is_set():
                self._menu_quit()
        return 0

    def _run_headless(self) -> int:
        """无托盘：前台守到用户 Ctrl+C，或在后端异常退出时结束。"""
        # Windows 上服务管理器/taskkill 发的是 SIGTERM（TerminateProcess 则是硬杀），
        # 这里统一转成 KeyboardInterrupt，让 finally 里的收尾逻辑一定跑到——
        # 否则关闭启动器会留下一个占着 8000 端口的孤儿后端进程。
        def _graceful(_sig, _frm):
            raise KeyboardInterrupt

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _graceful)
                except (ValueError, OSError):
                    pass
        print(f"AIMS 已启动：{self.supervisor.ui_url()}（Ctrl+C 退出）", flush=True)
        try:
            while not self._exit.is_set():
                proc = self.supervisor.backend
                if proc is not None and proc.poll() is not None:
                    logger.error("backend exited unexpectedly with %s", proc.returncode)
                    if getattr(sys, "frozen", False):
                        self._fail("AIMS 后端已退出",
                                   f"后端进程异常退出（代码 {proc.returncode}）。\n日志：{self.log_path}")
                    return 4
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self._menu_quit()
        return 0


def _version() -> str:
    try:
        from app.deploy import APP_VERSION

        return APP_VERSION
    except Exception:  # noqa: BLE001
        return "0.0.0"


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="AIMS", description="AIMS 桌面启动器")
    parser.add_argument("--root", help="安装根目录（默认：本程序所在目录）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开主界面")
    parser.add_argument("--headless", action="store_true", help="不用托盘，前台运行（排障）")
    parser.add_argument("--minimized", action="store_true", help="开机启动用：不开界面")
    parser.add_argument("--diagnose", action="store_true", help="打印诊断信息后退出")
    parser.add_argument("--status", action="store_true", help="以 JSON 打印运行状态后退出")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve() if args.root else _default_root()
    os.environ.setdefault("AIMS_ROOT", str(root))

    if args.version:
        print(f"AIMS {_version()}")
        return 0
    if args.diagnose:
        from app.deploy import doctor

        result = doctor.run(root=root, api_port=8000)
        print(doctor.report_text(result))
        return 0 if result["ok"] else 4
    if args.status:
        sup = Supervisor(root)
        try:
            sup.load()
        except SupervisorError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 3
        print(json.dumps(sup.status(), ensure_ascii=False, indent=2))
        return 0

    _setup_logging(root)
    win.set_dpi_aware()
    logger.info("launcher start: argv=%s root=%s frozen=%s", argv, root, getattr(sys, "frozen", False))

    lock = win.SingleInstance()
    if not lock.acquire():
        logger.info("another launcher instance is running; opening existing UI")
        port = 8000
        try:
            from app.deploy import conf
            from app.deploy.paths import load_layout

            port = int(conf.load_env(load_layout(root)).get("PORT", 8000) or 8000)
        except Exception:  # noqa: BLE001
            pass
        win.open_app_window(f"http://127.0.0.1:{port}/ui")
        return 0
    # 第二实例自己退出时不要释放别人的锁；只有拿到锁的进程才 release
    try:
        app = App(root, open_ui=not (args.no_browser or args.minimized), tray=not args.headless)
        return app.run()
    except Exception as exc:  # noqa: BLE001
        logger.exception("fatal launcher error")
        win.message_box("AIMS 启动器异常", f"{exc}\n\n日志：{root / 'aims_data' / 'logs' / 'launcher.log'}",
                        error=True)
        return 9
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
