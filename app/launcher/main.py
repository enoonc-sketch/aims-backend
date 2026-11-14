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
# 启动器偏好（aims_data/launcher.json）：托盘行为 + 显示/隐藏主窗口的热键
# --------------------------------------------------------------------------- #
LAUNCHER_PREFS_NAME = "launcher.json"
DEFAULT_TOGGLE_HOTKEY = "ctrl+alt+a"


def load_launcher_prefs(path: Path) -> dict:
    """读启动器偏好：文件缺失/损坏/值非法时回默认值，绝不因偏好文件坏了而起不来。"""
    prefs = {"close_to_tray_notified": False, "toggle_hotkey": DEFAULT_TOGGLE_HOTKEY, "alarm_notify": True}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if isinstance(data.get("close_to_tray_notified"), bool):
                prefs["close_to_tray_notified"] = data["close_to_tray_notified"]
            if isinstance(data.get("alarm_notify"), bool):
                prefs["alarm_notify"] = data["alarm_notify"]
            hotkey = data.get("toggle_hotkey")
            if isinstance(hotkey, str) and win.parse_hotkey(hotkey):
                prefs["toggle_hotkey"] = hotkey.strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return prefs


def save_launcher_prefs(path: Path, prefs: dict) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not save launcher prefs: %s", path)
        return False


def show_shortcuts_window(app: "App") -> None:
    """“用户快捷键”小窗口：列出本应用的快捷键，可改显示/隐藏主窗口的热键。

    托盘回调跑在后台线程，窗口在自己的守护线程里跑 mainloop；tkinter
    不可用时退回消息框（只读列出快捷键和当前热键）。
    """
    if getattr(app, "_shortcuts_open", False):
        return
    app._shortcuts_open = True

    def _run() -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("用户快捷键")
            root.geometry("440x300")
            try:
                root.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
            font = ("Microsoft YaHei UI", 10)
            tk.Label(root, text="显示 / 隐藏主窗口", font=("Microsoft YaHei UI", 11, "bold")).pack(
                anchor="w", padx=16, pady=(14, 4))
            row = tk.Frame(root)
            row.pack(fill="x", padx=16)
            entry = tk.Entry(row, font=font, width=22)
            entry.insert(0, str(app.prefs.get("toggle_hotkey") or DEFAULT_TOGGLE_HOTKEY))
            entry.pack(side="left")
            status = tk.Label(root, text="", font=("Microsoft YaHei UI", 9), fg="#666")
            status.pack(anchor="w", padx=16, pady=(4, 0))

            def _save() -> None:
                text = entry.get().strip()
                if app._apply_hotkey(text):
                    status.config(text=f"已保存：{text.lower()}，按一次显示/隐藏主窗口", fg="#16a34a")
                else:
                    status.config(text="格式不对，例如 ctrl+alt+a（要带 ctrl/alt/shift/win 其一）", fg="#dc2626")

            tk.Button(row, text="保存", font=font, command=_save).pack(side="left", padx=(8, 0))
            tk.Label(root, text="其它快捷键", font=("Microsoft YaHei UI", 11, "bold")).pack(
                anchor="w", padx=16, pady=(14, 4))
            tk.Label(root, text="回车：在登录、搜索、对话、改密码输入框中确认",
                     font=font, justify="left").pack(anchor="w", padx=16)
            tk.Label(root, text="托盘：右键托盘图标打开菜单，可打开主界面、备份、诊断、退出",
                     font=font, justify="left", wraplength=400).pack(anchor="w", padx=16, pady=(4, 0))
            tk.Button(root, text="关闭", font=font, command=root.destroy).pack(pady=16)
            root.mainloop()
        except Exception:  # noqa: BLE001
            logger.exception("shortcuts window failed")
            try:
                win.message_box("用户快捷键",
                                f"显示 / 隐藏主窗口：{app.prefs.get('toggle_hotkey') or DEFAULT_TOGGLE_HOTKEY}\n"
                                "回车：在登录、搜索、对话、改密码输入框中确认\n"
                                "托盘：右键托盘图标打开菜单")
            except Exception:  # noqa: BLE001
                pass
        finally:
            app._shortcuts_open = False

    threading.Thread(target=_run, name="aims-shortcuts-window", daemon=True).start()


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
        self._shortcuts_open = False
        self._hotkey_listener = None
        self._ui_state: dict | None = None      # 托盘菜单显示的登录状态；None = 取不到
        self._alarms: dict | None = None        # 最近一次取到的报警摘要；None = 取不到
        self._alarm_baseline: int | None = None  # 已经“见过”的最大报警 id；首次取到时只记下，不弹旧报警
        self.prefs_path = root / "aims_data" / LAUNCHER_PREFS_NAME
        self.prefs = load_launcher_prefs(self.prefs_path)

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
        # 后端由本次启动拉起时，交给看门狗盯着：崩溃能在日志里看见，并有限次自愈。
        self._start_backend_watchdog()
        self._start_hotkey_listener()
        self._start_ui_state_poller()
        # 界面只用系统自带的 Edge --app 窗口打开（不再用内嵌原生窗口）。
        if self.open_ui:
            url = self.supervisor.ui_url()
            how, proc = win.open_app_window_proc(url)
            logger.info("UI opened (%s): %s", how, url)
            if proc is not None:
                # 窗口关了应用本来就留在托盘；第一次关时提醒一声。
                self._watch_browser_window(proc)
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

    # ------------------------------------------------------- 界面登录状态（托盘显示）
    def _fetch_ui_state(self, with_alarms: bool = False) -> dict | None:
        """向后端问一次“界面现在谁登录着”。取不到（后端不是本启动器拉起的、刚重启…）返回 None。"""
        import json
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.supervisor.api_port}/api/v1/auth/ui-state" + ("?alarms=1" if with_alarms else ""),
            headers={"X-AIMS-Launcher": self.supervisor.launcher_token})
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:  # noqa: S310 - 本机回环
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _refresh_ui_state(self, with_alarms: bool = False) -> None:
        state = self._fetch_ui_state(with_alarms)
        alarms_seen = isinstance(state, dict) and "alarms" in state
        alarms = state.pop("alarms", None) if isinstance(state, dict) else None
        changed = state != self._ui_state
        self._ui_state = state
        if alarms_seen:
            changed = self._handle_alarms(alarms) or changed
        elif state is None and self._alarms is not None:   # 后端连不上了：别一直显示旧的报警数
            self._alarms = None
            changed = True
        if changed and self.icon is not None:
            try:
                self.icon.title = self._tray_title()
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def _start_ui_state_poller(self) -> None:
        def loop() -> None:
            tick = 0
            while not self._exit.wait(5.0):
                tick += 1
                self._refresh_ui_state(with_alarms=(tick % 3 == 0))     # 登录状态每 5 秒；报警要查库，每 15 秒

        self._refresh_ui_state(with_alarms=True)
        threading.Thread(target=loop, name="aims-ui-state", daemon=True).start()

    def _tray_title(self) -> str:
        st = self._ui_state
        base = (f"AIMS 设备智能运维系统 — 已登录：{st.get('username', '')}"
                if st and st.get("logged_in") else "AIMS 设备智能运维系统 — 未登录")
        n = (self._alarms or {}).get("active", 0)
        return base + (f" · 报警 {n}" if n else "")

    def _handle_alarms(self, alarms: dict | None) -> bool:
        """吃进一份新的报警摘要；有新报警就弹气泡。返回显示内容（菜单 / 提示文字）是否变了。"""
        changed = alarms != self._alarms
        self._alarms = alarms
        if not isinstance(alarms, dict):
            return changed
        max_id = int(alarms.get("max_id") or 0)
        if self._alarm_baseline is None:          # 第一次取到：只记下现状，不把启动前就有的报警当新报警弹
            self._alarm_baseline = max_id
            return changed
        if max_id > self._alarm_baseline:
            fresh = [a for a in alarms.get("latest", []) if int(a.get("id", 0)) > self._alarm_baseline]
            self._alarm_baseline = max_id
            if fresh and self.prefs.get("alarm_notify", True):
                self._notify_alarm(fresh)
        return changed

    def _notify_alarm(self, fresh: list) -> None:
        critical = any(a.get("severity") == "critical" for a in fresh)
        text = str(fresh[0].get("message") or "有新的报警")
        if len(fresh) > 1:
            text += f"（另有 {len(fresh) - 1} 条）"
        win.notify(self.icon, "AIMS 严重报警" if critical else "AIMS 新报警", text)

    def _alarm_text(self, _item=None) -> str:
        a = self._alarms
        if not isinstance(a, dict):
            return "当前报警：未获取"
        n, c = int(a.get("active") or 0), int(a.get("critical") or 0)
        if not n:
            return "当前报警：无未处理"
        return f"当前报警：{n} 条未处理" + (f"（严重 {c}）" if c else "")

    def _menu_open_alarms(self) -> None:
        """托盘“当前报警”：直接打开报警页（Edge --app 新窗口；未登录会先去登录，登录后再跳过来）。"""
        win.open_app_window(self.supervisor.ui_url() + "#alarm")

    def _menu_toggle_alarm_notify(self) -> None:
        self.prefs["alarm_notify"] = not self.prefs.get("alarm_notify", True)
        save_launcher_prefs(self.prefs_path, self.prefs)

    def _login_status_text(self, _item=None) -> str:
        st = self._ui_state
        if st is None:
            return "登录状态：未获取"
        return "登录状态：已登录" if st.get("logged_in") else "登录状态：未登录"

    def _login_user_text(self, _item=None) -> str:
        st = self._ui_state
        if not st or not st.get("logged_in"):
            return "登录人：—"
        name = st.get("username", "")
        full = st.get("full_name") or ""
        return f"登录人：{name}" + (f"（{full}）" if full and full != name else "")

    def _login_permission_text(self, _item=None) -> str:
        st = self._ui_state
        if not st or not st.get("logged_in"):
            return "使用权限：—"
        return f"使用权限：{st.get('permission') or '—'}"

    # ------------------------------------------------------------------ 菜单
    def menu_items(self):
        from app.launcher.tray_win32 import Menu, MenuItem as Item

        # 登录信息（只读，每次打开菜单取最近一次上报）
        yield Item(self._login_status_text, None, enabled=False)
        yield Item(self._login_user_text, None, enabled=False)
        yield Item(self._login_permission_text, None, enabled=False)
        yield Menu.SEPARATOR
        yield Item(self._alarm_text, self._menu_open_alarms)           # 点一下直达报警页
        yield Menu.SEPARATOR
        yield Item("打开 AIMS 主界面", self._menu_open_ui, default=True)
        yield Item("设置", self._menu_open_settings)
        yield Item("用户快捷键", self._menu_shortcuts)
        yield Item("立即备份数据库", self._menu_backup)
        yield Item("打开数据目录", lambda: self._open_path(self.supervisor.layout.data_dir))
        yield Item("打开日志目录", lambda: self._open_path(self.supervisor.layout.logs_dir))
        yield Item("生成诊断报告", self._menu_doctor)
        yield Item("重启服务", self._menu_restart)
        yield Item("开机自动启动", self._menu_autostart, checked=lambda _i: win.autostart_enabled())
        yield Item("新报警弹出气泡通知", self._menu_toggle_alarm_notify,
                   checked=lambda _i: bool(self.prefs.get("alarm_notify", True)))
        yield Menu.SEPARATOR
        yield Item(f"版本 {_version()}", lambda: win.message_box(
            PRODUCT_NAME, f"版本：{_version()}\n安装目录：{self.root}\n数据目录：{self.supervisor.layout.data_dir}\n"
                        f"界面地址：{self.supervisor.ui_url()}\n日志：{self.log_path}"))
        yield Item("退出程序", self._menu_quit)

    # ------------------------------------------------------- 主窗口（显示/隐藏）
    def _settings_url(self) -> str:
        return self.supervisor.ui_url() + "#sys/settings"

    def _on_close_to_tray(self) -> None:
        """浏览器窗口关了：应用继续留在托盘跑；第一次提醒用户一声。"""
        if not self.prefs.get("close_to_tray_notified"):
            self.prefs["close_to_tray_notified"] = True
            save_launcher_prefs(self.prefs_path, self.prefs)
            if self.icon is not None:
                win.notify(self.icon, PRODUCT_NAME,
                           "AIMS 仍在后台运行：点托盘图标可重新打开主界面，选“退出程序”才真正退出")
        logger.info("main window closed to tray")

    def _menu_open_ui(self) -> None:
        win.open_app_window(self.supervisor.ui_url())

    def _menu_open_settings(self) -> None:
        """托盘“设置”：直接打开系统设置 → 设置页。"""
        win.open_app_window(self._settings_url())

    def _menu_shortcuts(self) -> None:
        show_shortcuts_window(self)

    def _toggle_main_window(self) -> None:
        """全局热键：打开主界面（Edge --app 窗口）。"""
        win.open_app_window(self.supervisor.ui_url())

    def _watch_browser_window(self, proc) -> None:
        """浏览器窗口模式：盯着浏览器进程，窗口关了（且不是启动后几秒内的
        “交接退出”）就按“最小化到托盘”提醒一次。"""
        def _wait() -> None:
            started = time.time()
            try:
                proc.wait()
            except Exception:  # noqa: BLE001
                return
            if self._exit.is_set():
                return
            if time.time() - started < 5.0:
                return  # 新进程立刻退出 = 把请求交给已在跑的浏览器，不是用户关窗口
            self._on_close_to_tray()

        threading.Thread(target=_wait, name="aims-browser-window-watch",
                         daemon=True).start()

    def _start_hotkey_listener(self) -> None:
        if self._hotkey_listener is not None:
            return
        parsed = win.parse_hotkey(str(self.prefs.get("toggle_hotkey") or ""))
        if parsed is None:
            return
        listener = win.HotkeyListener(parsed, self._toggle_main_window)
        if listener.start():
            self._hotkey_listener = listener

    def _apply_hotkey(self, text: str) -> bool:
        """快捷键窗口保存后调用：校验、持久化、重新注册。返回是否成功。"""
        parsed = win.parse_hotkey(text)
        if parsed is None:
            return False
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener = None
        self.prefs["toggle_hotkey"] = text.strip().lower()
        save_launcher_prefs(self.prefs_path, self.prefs)
        self._start_hotkey_listener()
        return True

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
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._hotkey_listener = None
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

    # ------------------------------------------------------ 后端异常退出监控
    #: 自动重启上限；超过即认为不是偶发崩溃，停止重试并告知用户（防止"崩溃-重启"死循环）
    MAX_BACKEND_RESTARTS = 3
    #: 刚启动就崩（< 该秒数）视为不稳定，退避更长、更快耗尽重试次数
    QUICK_CRASH_SECONDS = 60.0

    @staticmethod
    def _restart_policy(restarts: int, since_last_start: float) -> tuple[str, float]:
        """崩溃后的处置策略（纯函数，便于测试）。

        返回 ("restart", 延迟秒数) 或 ("give_up", 0)。
        """
        if restarts >= App.MAX_BACKEND_RESTARTS:
            return ("give_up", 0.0)
        if since_last_start < App.QUICK_CRASH_SECONDS:
            # 1.5s / 3s / 6s 退避：给瞬时故障（端口抢占、OOM 回收）留出恢复时间
            return ("restart", 1.5 * (2 ** restarts))
        return ("restart", 2.0)

    def _start_backend_watchdog(self) -> None:
        """所有界面模式通用的后端看门狗。

        以前只有 `--headless` 排障模式会轮询后端退出码；正式用户走的是
        浏览器窗口 / 托盘，后端一崩，窗口只会白屏，日志也没人看，启动器
        更不会把它拉起来——"后台服务异常"在需求里必须可发现、可恢复。
        """
        if not self.supervisor.started_backend_here:
            return  # 后端起自别的进程/服务：不归本启动器管，避免误杀误启
        if getattr(self, "_watchdog_started", False):
            return  # 托盘「重启服务」会重新调用 start()：复用同一个看门狗线程
        self._watchdog_started = True
        threading.Thread(target=self._watch_backend, name="aims-backend-watchdog",
                         daemon=True).start()

    def _watch_backend(self) -> None:
        restarts = 0
        started_at = time.time()
        while not self._exit.is_set():
            time.sleep(2.0)
            if self._exit.is_set():
                return
            proc = self.supervisor.backend
            if proc is None or proc.poll() is None:
                continue
            code = proc.returncode
            action, delay = self._restart_policy(restarts, time.time() - started_at)
            logger.error("backend exited unexpectedly (code=%s); policy=%s delay=%.1fs restarts=%s",
                         code, action, delay, restarts)
            if action == "give_up":
                self._fail(
                    "AIMS 后端已停止",
                    f"后端服务连续 {restarts} 次异常退出（最后一次退出代码 {code}），已停止自动恢复。\n\n"
                    f"日志：{self.supervisor.layout.logs_dir / 'backend.log'}\n\n"
                    "可在托盘菜单「重启服务」手动重试，或运行「AIMS 诊断报告」查看原因。",
                )
                return
            time.sleep(delay)
            if self._exit.is_set():
                return
            try:
                self.supervisor.start_backend()
            except Exception as exc:  # noqa: BLE001  (SupervisorError 已含日志尾巴)
                logger.error("backend auto-restart failed: %s", exc)
                restarts += 1
                started_at = time.time()
                continue
            restarts += 1
            started_at = time.time()
            logger.info("backend auto-restarted (attempt %s)", restarts)
            if self.icon is not None:
                try:
                    win.notify(self.icon, PRODUCT_NAME, "后端服务已自动重启")
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ 运行
    def run(self) -> int:
        if not self.start():
            if getattr(sys, "frozen", False) and "--quiet" not in sys.argv:
                pass
            return 3
        if not self.want_tray or not win.tray_available():
            if self.want_tray:
                logger.warning("tray unavailable on this platform — running without tray")
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
    parser.add_argument("--debug-console", action="store_true",
                        help="开发调试模式：给后端子进程分配可见控制台（正式用户路径不使用）")
    args = parser.parse_args(argv)

    # 调试模式经由环境变量传给后端子进程（Supervisor._spawn_env 会继承 os.environ），
    # 后端 run_standalone.py 检测到 AIMS_DEBUG_CONSOLE=1 时会 AllocConsole。
    # 启动器本体是 GUI 程序，不给自己分配控制台。
    if args.debug_console:
        os.environ["AIMS_DEBUG_CONSOLE"] = "1"

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
