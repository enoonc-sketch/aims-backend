#!/usr/bin/env python3
"""
Standalone entry point for AIMS Backend (PyInstaller EXE / source).

Fixes for EXE flash-exit:
  - Never use string target "app.main:app" under frozen mode
  - Catch all startup errors and pause so the console stays open
  - Write a crash log next to the EXE
  - Force SQLite + data dir next to the executable
"""
from __future__ import annotations

import os
import sys
import traceback
import ssl
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _base_dir() -> Path:
    # AIMS_ROOT wins over the executable location: it is what the installer, the
    # launcher and the ops scripts all set, and what makes the whole tree relocatable
    # (a support engineer can point at a copy of an installation without touching it).
    override = os.environ.get("AIMS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundled_pg(base: Path) -> bool:
    """Is this a managed install with its own PostgreSQL under <base>/pgsql/bin?

    Cheap test (no imports) because it decides which database the whole process
    uses: bundled PostgreSQL (offline installer) or the legacy SQLite file.
    """
    if os.environ.get("AIMS_MANAGED_RUNTIME", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("AIMS_DB_MODE", "").strip().lower() in ("sqlite", "external"):
        return False
    exe = "pg_ctl.exe" if os.name == "nt" else "pg_ctl"
    bindir = os.environ.get("AIMS_PG_BINDIR")
    if bindir:
        return (Path(bindir).expanduser() / exe).is_file()
    return (base / "pgsql" / "bin" / exe).is_file()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _watch_parent(server) -> None:
    """启动器死了，后端必须跟着退 —— 否则会留下占着 8000 端口的孤儿进程。

    正常退出走托盘菜单，会显式通知本进程关停；这条兜底路径针对的是启动器被
    任务管理器结束、被安装程序强杀、或自身崩溃的情况。父进程一消失，这里就让
    uvicorn 正常关停（关连接池、写日志），而不是让用户下次启动时看到
    "端口被占用"却找不到是谁占的。
    """
    raw = os.environ.get("AIMS_PARENT_PID", "").strip()
    if not raw.isdigit():
        return
    parent = int(raw)
    if parent <= 0 or not _pid_alive(parent):
        return

    def loop() -> None:
        import time as _t

        while True:
            _t.sleep(3.0)
            if not _pid_alive(parent):
                print(f"启动器 (PID {parent}) 已退出，AIMS 后端随之关闭。", flush=True)
                server.should_exit = True
                return

    threading.Thread(target=loop, name="aims-parent-watchdog", daemon=True).start()


def _prepare_env(base: Path) -> None:
    """Set env BEFORE importing app modules so Settings picks them up."""
    os.chdir(base)

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = str(sys._MEIPASS)
        if meipass not in sys.path:
            sys.path.insert(0, meipass)

    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    data_dir = base / "aims_data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Managed install: config/aims.env supplies DATABASE_URL etc. (applied in
    # _bootstrap_managed below). Only the legacy SQLite build defaults to a file.
    if not _bundled_pg(base):
        db_file = data_dir / "aims.db"
        db_url = "sqlite:///" + db_file.resolve().as_posix()
        os.environ.setdefault("DATABASE_URL", db_url)
    os.environ.setdefault("AUTO_INIT_DB", "true")
    # NOTE: SECRET_KEY / CORS_ORIGINS / TLS files are deliberately NOT defaulted here.
    # In development (APP_ENV != production) a missing SECRET_KEY is replaced by a random key
    # persisted next to the database, and an empty database gets a development admin
    # (ALLOW_DEV_ADMIN). In production both must be configured externally, and wildcard
    # CORS is never enabled.
    os.environ.setdefault("HOST", "127.0.0.1")
    os.environ.setdefault("PORT", "8000")


def _bootstrap_managed(base: Path):
    """Start the bundled PostgreSQL + reconcile the schema. Returns info or None."""
    if not _bundled_pg(base):
        return None
    from app.deploy import runtime

    return runtime.bootstrap(base, announce=lambda msg: print(msg, flush=True))



def _has_console() -> bool:
    """本进程是否真的有一个可用的控制台（GUI 子系统 EXE 平时没有）。"""
    if os.name != "nt":
        return sys.stdin is not None and sys.stdin.isatty()
    import ctypes

    return ctypes.windll.kernel32.GetConsoleWindow() != 0


def _attach_debug_console() -> None:
    """开发调试模式：给 GUI 子系统的 EXE 现场分配一个控制台窗口。

    触发条件（缺一不可）：打包成 EXE + 显式 AIMS_DEBUG_CONSOLE=1。
    正式用户路径（桌面快捷方式 / 启动器）永远不设这个变量，因此不会弹窗；
    开发人员用「AIMS 调试模式」快捷方式或 run_exe_debug.bat 查看输出。
    """
    if os.name != "nt":
        return  # 源码运行本来就有控制台，无需处理
    if os.environ.get("AIMS_DEBUG_CONSOLE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return  # 已经有控制台（例如源码运行）：不要重复分配
    ATTACH_PARENT_PROCESS = -1
    # 优先挂到父进程（开发人员从 cmd / run_exe_debug.bat 启动 EXE）的控制台；
    # 没有父控制台（双击调试快捷方式）再现场分配一个新窗口。
    if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        if not kernel32.AllocConsole():
            return
    # 把标准流重新接到新控制台上；崩溃日志/uvicorn 输出恢复可见。
    try:
        sys.stdin = open("CONIN$", "r", buffering=1)
        sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(sys.stdout.fileno(), 1)
        os.dup2(sys.stderr.fileno(), 2)
    except Exception:
        pass


def _redirect_missing_streams() -> None:
    """GUI 子系统 EXE（console=False）下 sys.stdin/stdout/stderr 全是 None。

    这不是摆设：uvicorn 的 ColourizedFormatter 在 configure_logging() 里调
    `sys.stdout.isatty()`，None 没有这个方法，`config.load()` 直接抛 ValueError，
    服务线程还没起就死了；threading 的异常钩子往 None 的 stderr 写也被吞掉，
    用户最后只看到一个"Press Enter to exit..."的空弹窗——1.0.0 正式版就是
    这样在真机上启动即崩溃的。这是 PyInstaller 官方文档记载的 windowed 模式
    陷阱，标准做法就是启动第一时间把缺失的流接到空设备（`logging` 的文件
    handler 与 aims_crash.log 不受影响，诊断信息仍在）。

    已有真控制台（源码运行 / 调试模式 / 从 cmd 启动）时不动；流存在时不动。
    """
    if _has_console():
        return
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        try:
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(os.devnull, mode, encoding="utf-8", errors="replace"))
        except Exception:
            pass


def _pause_dialog_text(detail: str = "") -> str:
    """GUI 弹窗里显示的文本：必须是真正的报错原因 + 崩溃日志位置。

    绝不能只是等回车的提示——1.0.0 的弹窗只显示 "Press Enter to exit..."，
    用户完全不知道发生了什么、日志在哪，排障无从下手。
    """
    body = detail.strip() or "AIMS Backend 启动失败。"
    return body + "\n\n按确定退出。"


def _pause(detail: str = "") -> None:
    """启动失败时停住进程让用户读到原因——但绝不能为此弹 CMD 窗口。

    * 有控制台（源码运行 / 调试模式 / 从 cmd 启动）：按回车退出；
    * GUI 子系统 EXE（正式用户路径）：用 Windows 原生 MessageBox 弹窗，
      点确定退出。原来的 `os.system("pause")` 会瞬间拉起一个 cmd.exe
      窗口，在 GUI 模式下正是"莫名其妙闪出黑框"的来源之一。

    `detail` 是真正的报错原因（含崩溃日志路径），调用点必须传；不传时
    弹窗只会显示一句"启动失败"，和 1.0.0 的空弹窗一样没用。
    """
    if os.name == "nt" and not _has_console():
        try:
            import ctypes

            MB_OK, MB_ICONERROR = 0x0, 0x10
            ctypes.windll.user32.MessageBoxW(
                None, _pause_dialog_text(detail), "AIMS Backend", MB_OK | MB_ICONERROR)
            return
        except Exception:
            pass
    try:
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to exit...")
        else:
            import time
            time.sleep(30)
    except Exception:
        try:
            import time
            time.sleep(30)
        except Exception:
            pass


def _write_crash_log(base: Path, exc: BaseException) -> Path:
    """Write aims_crash.log; return the path actually written (or the first choice).

    Installed under Program Files the folder next to the EXE is read-only for normal
    users, so fall back to aims_data (the installer makes it writable), then to TEMP.
    """
    import tempfile

    candidates = [base / "aims_crash.log", base / "aims_data" / "aims_crash.log",
                  Path(tempfile.gettempdir()) / "aims_crash.log"]
    for log_path in candidates:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("AIMS Backend crash log\n")
                f.write("=" * 60 + "\n")
                f.write(f"executable: {sys.executable}\n")
                f.write(f"cwd: {os.getcwd()}\n")
                f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
                f.write(f"DATABASE_URL: {os.environ.get('DATABASE_URL')}\n")
                f.write(f"sys.path[:5]: {sys.path[:5]}\n\n")
                f.write(traceback.format_exc())
            return log_path
        except Exception:
            continue
    return candidates[0]


def _open_ui_window(base_url: str, settings) -> bool:
    """Open the UI in a pywebview desktop window.

    Returns True only if a window was actually shown (webview.start() ran
    until the window was closed). Any failure falls back to the system
    browser and returns False; the server keeps running either way.
    """
    # TLS mode: WebView2 cannot show the self-signed cert (cert-error page
    # with no click-through), and the HTTPS middleware rejects plaintext
    # http with 403 (app/core/middleware.py:43-64). Keep the browser here,
    # where the user can click through the cert warning.
    if getattr(settings, "tls_direct", False) or getattr(settings, "https_required", False):
        print("TLS is on: opening the UI in the system browser (the desktop window needs plain http).")
        try:
            webbrowser.open(f"{base_url}/ui")
        except Exception:
            pass
        return False
    # Desktop window only for the frozen EXE; `python run_standalone.py`
    # from source keeps the old browser behaviour.
    if not getattr(sys, "frozen", False):
        try:
            webbrowser.open(f"{base_url}/ui")
        except Exception:
            pass
        return False
    try:
        import webview
    except ImportError:
        print("pywebview is not installed; opening the UI in the system browser.")
        try:
            webbrowser.open(f"{base_url}/ui")
        except Exception:
            pass
        return False
    try:
        try:  # pywebview 5+: allow <a download> (CSV export) to show a save dialog
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        window = webview.create_window(
            "AIMS Backend", f"{base_url}/ui",
            width=1280, height=800,
            resizable=True, text_select=True,
            # NOTE: do NOT set on_top=True — pywebview 6.2.1 has a known
            # EdgeChromium deadlock with on_top under activation load
            # (r0x0r/pywebview#1823). sing-box sets on_top=True; do not copy it.
        )
        webview.start()
        return True
    except Exception as e:
        print(f"Desktop window failed ({e}); opening the UI in the system browser.")
        print("If WebView2 Runtime is missing, install it from:")
        print("https://developer.microsoft.com/microsoft-edge/webview2/")
        try:
            webbrowser.open(f"{base_url}/ui")
        except Exception:
            pass
        return False


def _sqlite_db_path() -> Path | None:
    """The SQLite file behind DATABASE_URL, or None for other databases / in-memory."""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):].split("?", 1)[0]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


def _backup_database(now: float | None = None) -> Path | None:
    """Copy the SQLite DB to <data dir>/backups/aims-YYYYmmdd-HHMMSS.db at startup.

    At most one backup per 12 h, newest AIMS_BACKUP_KEEP (default 7) are kept,
    AIMS_BACKUP_KEEP=0 turns it off. Uses sqlite3's online backup API, so the copy is
    consistent even if the file is in use. Never raises: a failed backup must not stop startup.
    """
    import sqlite3

    try:
        keep = int(os.environ.get("AIMS_BACKUP_KEEP", "7") or 0)
    except ValueError:
        keep = 7
    db = _sqlite_db_path()
    if keep <= 0 or db is None or not db.is_file() or db.stat().st_size == 0:
        return None
    now = time.time() if now is None else now
    try:
        bdir = db.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        existing = sorted(bdir.glob("aims-*.db"))
        if existing and now - existing[-1].stat().st_mtime < 12 * 3600:
            return None
        dest = bdir / ("aims-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + ".db")
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        for old in sorted(bdir.glob("aims-*.db"))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        print(f"  Backup   : {dest}")
        return dest
    except Exception as e:  # noqa: BLE001
        print(f"  Backup skipped ({e})")
        return None


def _setup_file_logging(base: Path):
    """Also write logs to <data dir>/logs/aims.log (2 MB x 3 files); AIMS_LOG_FILE=0 disables.

    Must run AFTER uvicorn.Config(...): that call reconfigures the uvicorn loggers.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    if os.environ.get("AIMS_LOG_FILE", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    db = _sqlite_db_path()
    log_dir = (db.parent if db else base / "aims_data") / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "aims.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  Log file disabled ({e})")
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    # Root gets it first. uvicorn's own loggers are attached only if they do NOT propagate to
    # root (uvicorn.Config sets propagate=False for "uvicorn" and "uvicorn.access"), otherwise
    # every line would be written twice. "uvicorn.error" propagates into "uvicorn".
    handler._aims_file = True
    for name in ("", "uvicorn", "uvicorn.access"):
        lg = logging.getLogger(name)
        if name and lg.propagate:
            continue
        if not any(getattr(h, "_aims_file", False) for h in lg.handlers):
            lg.addHandler(handler)
    if logging.getLogger().level > logging.INFO or logging.getLogger().level == logging.NOTSET:
        logging.getLogger().setLevel(logging.INFO)
    return handler


_MUTEX_NAME = "AIMS-Backend-Mutex"
_MUTEX_HANDLE = None  # kept for the life of the process; Windows frees it on exit


def _hold_app_mutex():
    """Windows only: create a named mutex so the installer (AppMutex in AIMS-Backend.iss)
    can tell AIMS is running and ask the user to close it before upgrading / uninstalling.
    Returns the handle (or None elsewhere / on failure). Never raises."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return None
    try:
        import ctypes

        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    except Exception:  # noqa: BLE001
        _MUTEX_HANDLE = None
    return _MUTEX_HANDLE


def _list_backups() -> list:
    """Automatic backups, newest first (pre-restore safety copies are not listed)."""
    db = _sqlite_db_path()
    if db is None:
        return []
    return sorted((db.parent / "backups").glob("aims-*.db"), reverse=True)


def _restore_backup(which: str = "latest") -> int:
    """`AIMS-Backend.exe restore [latest|<file name>]`: put a backup back as the live database.

    Refuses while AIMS is running. The current database is first copied to
    backups/pre-restore-<time>.db (never pruned), and the backup is verified with
    PRAGMA integrity_check before anything is replaced.
    """
    import sqlite3

    db = _sqlite_db_path()
    if db is None:
        print("Restore only works with the built-in SQLite database.")
        return 1
    port = os.environ.get("PORT", "8000")
    for scheme in ("http", "https"):
        if _probe_health(f"{scheme}://127.0.0.1:{port}", scheme) is not None:
            print(f"AIMS appears to be running on port {port}. Close it first, then run restore again.")
            return 1
    backups = _list_backups()
    if which in ("", "latest"):
        if not backups:
            print("No backups found.")
            return 1
        chosen = backups[0]
    else:
        chosen = next((b for b in backups if b.name == Path(which).name), None)
        if chosen is None:
            print(f"Backup not found: {which}  (run `backups` to list them)")
            return 1
    try:
        check = sqlite3.connect(f"file:{chosen.as_posix()}?mode=ro", uri=True)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            check.close()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        print(f"{chosen.name} is not a healthy SQLite database; nothing was changed.")
        return 1
    try:
        safety = None
        if db.is_file():
            safety = chosen.parent / ("pre-restore-" + time.strftime("%Y%m%d-%H%M%S") + ".db")
            src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(str(safety))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        tmp = db.with_name(db.name + ".restoring")
        src = sqlite3.connect(f"file:{chosen.as_posix()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        for side in ("-wal", "-shm"):
            try:
                os.remove(str(db) + side)
            except OSError:
                pass
        os.replace(tmp, db)
    except Exception as e:  # noqa: BLE001
        print(f"Restore failed ({e}); the original database was not replaced.")
        return 1
    print(f"Restored {chosen.name} -> {db}")
    if safety is not None:
        print(f"Previous database kept as {safety}")
    return 0


def _probe_health(base_url: str, scheme: str, timeout: float = 0.5):
    """GET {base_url}/health without any proxy (a system proxy would break localhost).

    Returns the parsed JSON dict (``{}`` if the body is not JSON) when something answers
    200, otherwise None. /health needs no auth and is exempt from HTTPS enforcement, so
    this also works in TLS mode (self-signed cert accepted for this local probe only).
    """
    import json

    try:
        handlers = [urllib.request.ProxyHandler({})]
        if scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(urllib.request.Request(base_url + "/health"), timeout=timeout) as r:
            if r.status != 200:
                return None
            try:
                data = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                data = {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return None


def main() -> int:
    # GUI 子系统 EXE：显式调试模式（AIMS_DEBUG_CONSOLE=1）时先分配控制台，
    # 让部署/启动阶段的 print 与 traceback 恢复可见；普通用户路径不设该变量。
    if getattr(sys, "frozen", False):
        _attach_debug_console()
    # 没有控制台时（GUI 子系统 EXE）标准流全是 None：uvicorn 配日志时调
    # sys.stdout.isatty() 会直接炸掉服务线程。先接到空设备，再干别的。
    _redirect_missing_streams()
    base = _base_dir()

    # `AIMS-Backend.exe deploy <cmd>` — installer / operations CLI. Handled before
    # _prepare_env so it can drive the cluster without the app's defaults in the way.
    if len(sys.argv) > 1 and sys.argv[1] == "deploy":
        os.chdir(base)
        for p in ({str(base), str(getattr(sys, "_MEIPASS", ""))} - {""}):
            if p not in sys.path:
                sys.path.insert(0, p)
        from app.deploy.cli import main as deploy_main
        return deploy_main(sys.argv[2:])

    _prepare_env(base)

    # 子命令(不启动服务): setup 向导 / 用户管理 / 生成密钥。
    # 合并自 secure 包 run_standalone.py 的分发, 接在 0.5.0 的 app.cli 包上。
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "setup":
            from app.cli.setup import main as setup_main
            return setup_main(sys.argv[2:], base=base)
        if cmd == "generate-secret":
            import secrets
            print(secrets.token_urlsafe(64))
            return 0
        if cmd in ("backups", "restore") and _bundled_pg(base):
            # Managed install: backups are pg_dump archives, handled by app/deploy.
            from app.deploy.cli import main as deploy_main
            return deploy_main([cmd] + sys.argv[2:])
        if cmd == "backups":
            items = _list_backups()
            if not items:
                print("No backups yet (they are created automatically at startup).")
            for b in items:
                print(f"{b.name}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(b.stat().st_mtime))}  {b.stat().st_size // 1024} KB")
            return 0
        if cmd == "restore":
            return _restore_backup(sys.argv[2] if len(sys.argv) > 2 else "latest")
        if cmd in ("create-admin", "reset-password", "activate", "list"):
            from app.cli.manage_users import main as manage_users_main
            return manage_users_main()

    print("=" * 60)
    print("  AIMS Backend — starting...")
    print(f"  Base dir : {base}")
    print("=" * 60)
    print()

    # Bundled PostgreSQL: start it and bring the schema up to date before the app
    # imports (Settings reads DATABASE_URL at import time).
    runtime_info = None
    try:
        runtime_info = _bootstrap_managed(base)
    except Exception as exc:  # noqa: BLE001  (DeploymentError carries the user-facing text)
        print("\n" + "!" * 60)
        print("  数据库启动失败 — AIMS 无法运行")
        print("!" * 60)
        print(str(exc))
        detail = ""
        try:
            from app.deploy import runtime as rt
            from app.deploy.paths import load_layout

            detail = rt.diagnostic_tail(load_layout(base))
        except Exception:
            pass
        if detail:
            print("\n最近的日志：\n" + detail)
        if os.name == "nt":
            try:
                from app.deploy import runtime as rt

                rt.show_error("AIMS Backend — 数据库启动失败", str(exc))
            except Exception:
                pass
        return 3

    print(f"  Database : {os.environ.get('DATABASE_URL')}")
    print()

    try:
        # Rebuild Settings from env we just set (before any DB access)
        from app.core.config import reload_settings
        settings = reload_settings()
        import app.core.config as config_mod
        config_mod.settings = settings

        from app.main import app
        import uvicorn

        host = settings.HOST or "127.0.0.1"
        port = int(settings.PORT or 8000)
        # direct TLS (uvicorn terminates) or behind a trusted TLS proxy => advertise https
        scheme = "https" if (settings.tls_direct or settings.https_required) else "http"
        base_url = f"{scheme}://127.0.0.1:{port}"

        print(f"  App      : {settings.APP_NAME}")
        print(f"  UI       : {base_url}/ui")
        if settings.docs_enabled:
            print(f"  API Docs : {base_url}/docs")
        print(f"  Health   : {base_url}/health")
        print("=" * 60)
        print("Press Ctrl+C to stop the server.")
        print()

        # Single instance: if an AIMS server already answers on this port (the user double-clicked
        # twice), do not start a second server (it would die with "address already in use");
        # just show a window onto the running one. We do not own that server, so we never stop it.
        existing = _probe_health(base_url, scheme)
        if existing is not None:
            if existing.get("app") == settings.APP_NAME:
                print(f"{settings.APP_NAME} is already running at {base_url}; opening it instead of starting a second server.")
                if os.environ.get("AIMS_OPEN_BROWSER", "1") == "1":
                    _open_ui_window(base_url, settings)
                return 0
            print(f"Port {port} is already used by another service. Set PORT in .env (or free the port) and start again.")
            if getattr(sys, "frozen", False):
                _pause(f"端口 {port} 已被其他程序占用，AIMS 无法启动。\n请关闭占用该端口的程序，或在 .env 里改 PORT 再启动。")
            return 1

        _hold_app_mutex()  # lets the installer detect a running AIMS (after the single-instance check)
        _backup_database()  # before anything touches the DB (after the single-instance check)

        # AIMS_OPEN_BROWSER semantics: "1" (default) = open the UI (a desktop
        # window in the frozen EXE, the system browser otherwise); "0" = open
        # nothing, just serve (headless / remote use).
        # TLS mode and source runs never open the window, see _open_ui_window().
        # Pass app object, NOT "app.main:app" string (breaks in frozen EXE)
        tls_kwargs = {}
        if settings.tls_direct:
            tls_kwargs = {"ssl_certfile": settings.SSL_CERTFILE, "ssl_keyfile": settings.SSL_KEYFILE}
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            reload=False,
            log_level="info",
            workers=1,
            # X-Forwarded-Proto/For are honoured ONLY from these peers (TLS-terminating proxy)
            proxy_headers=True,
            forwarded_allow_ips=",".join(settings.trusted_proxy_list) or "127.0.0.1",
            **tls_kwargs,
        )
        server = uvicorn.Server(config)
        _setup_file_logging(base)

        # uvicorn runs in a daemon thread: the desktop window needs the main
        # thread for its GUI loop. When the main thread ends (window closed),
        # the process exits and the daemon thread dies with it.
        # (Same pattern as the sing-box generator, app.py:279.)
        #
        # 线程里的异常不会冒到主线程：threading 的异常钩子往 stderr 写，而 GUI
        # 子系统下 stderr 是 None，异常会被悄悄吞掉。包一层把它记下来，启动
        # 失败时才能在弹窗里告诉用户真正的原因（1.0.0 的 uvicorn 日志配置
        # 崩溃就是这样丢的：config.load() 里 ColourizedFormatter 调
        # sys.stdout.isatty()，None 没有这个方法）。
        _server_error: list[BaseException] = []

        def _run_server_guarded() -> None:
            try:
                server.run()
            except BaseException as exc:  # noqa: BLE001 — 记下来给 not-healthy 分支用
                _server_error.append(exc)

        server_thread = threading.Thread(target=_run_server_guarded, daemon=True)
        server_thread.start()
        _watch_parent(server)

        # Wait until the server really answers /health (max 15 s, no proxy:
        # a system proxy would break localhost). Same pattern as sing-box
        # (app.py:285-296). /health needs no auth and is exempt from HTTPS
        # enforcement, so this also works in TLS mode.
        healthy = False
        deadline = time.time() + 15
        while time.time() < deadline:
            if not server_thread.is_alive():
                break  # uvicorn exited already (e.g. port in use): no point waiting 15 s
            if _probe_health(base_url, scheme) is not None:
                healthy = True
                break
            time.sleep(0.15)
        if not healthy:
            # 1.0.0 以前这里不写崩溃日志、弹窗也不带原因，用户只能看到
            # "Press Enter to exit..."。现在：线程里记下的异常写进崩溃日志，
            # 弹窗显示真正的原因 + 日志路径。
            detail = ""
            if _server_error:
                exc = _server_error[0]
                log_path = _write_crash_log(base, exc)
                detail = f"服务启动失败：{exc}\n\n崩溃日志：{log_path}"
                print(f"The server thread died at startup: {exc!r} (crash log: {log_path})")
            elif not server_thread.is_alive():
                print(f"The server stopped right after starting. Most likely port {port} is already in use "
                      "by another program: set PORT in .env or free the port. See the messages above.")
                detail = (f"服务启动后立即退出。最可能的原因是端口 {port} 已被其他程序占用："
                          f"请关闭占用该端口的程序，或在 .env 里改 PORT 再启动。")
            else:
                print("Server did not become ready in 15 s.")
                print(f"If it actually started, open manually: {base_url}/ui")
                detail = ("服务 15 秒内没有就绪。\n"
                          f"如果它其实已经起来了，直接打开：{base_url}/ui")
            server.should_exit = True
            if getattr(sys, "frozen", False):
                _pause(detail)
            return 1

        opened_window = False
        if os.environ.get("AIMS_OPEN_BROWSER", "1") == "1":
            opened_window = _open_ui_window(base_url, settings)

        if not opened_window:
            # No window (source run, TLS mode, AIMS_OPEN_BROWSER=0, or the
            # window fell back to the browser): wait for the server thread
            # until Ctrl+C, exactly like the old blocking uvicorn.run().
            try:
                while server_thread.is_alive():
                    server_thread.join(timeout=1)
            except KeyboardInterrupt:
                print("\nServer stopped.")
        # Window was closed, or the wait loop ended: stop the server.
        server.should_exit = True
        server_thread.join(timeout=5)
        if runtime_info and runtime_info.get("managed"):
            # Only stops PostgreSQL in process mode (service-mode clusters stay up).
            try:
                from app.deploy import runtime as rt

                rt.shutdown(base, started_here=bool(runtime_info.get("started_here")))
            except Exception:
                pass
        return 0

    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0
    except ValueError as exc:
        # Settings validation failed (missing/weak SECRET_KEY, wildcard CORS, no HTTPS in
        # production ...). Show the reason plainly instead of a stack trace.
        print("\n" + "!" * 60)
        print("  CONFIGURATION ERROR — refusing to start")
        print("!" * 60)
        print(str(exc))
        print()
        print("Provide the settings via environment variables or an untracked .env file")
        print("next to the executable (see .env.example). Generate a SECRET_KEY with:")
        print("  python -m app.cli gen-secret")
        print("Or run the one-step setup wizard (key + HTTPS cert + first admin):")
        print("  python run_standalone.py setup")
        log_path = _write_crash_log(base, exc)
        _pause(f"配置错误，AIMS 拒绝启动：\n{exc}\n\n崩溃日志：{log_path}\n"
                "可用环境变量或 .env 文件提供配置（见 .env.example），或运行：\n"
                "  python run_standalone.py setup")
        return 2
    except Exception as exc:
        print("\n" + "!" * 60)
        print("  STARTUP FAILED")
        print("!" * 60)
        traceback.print_exc()
        log_path = _write_crash_log(base, exc)
        print()
        print(f"Crash log: {log_path}")
        print("Common fixes:")
        print("  1. Check the message above and your .env / environment variables")
        print("  2. Ensure port 8000 is free")
        print("  3. Run from cmd:  AIMS-Backend.exe")
        print("  4. Re-build with build_exe.bat")
        print("!" * 60)
        _pause(f"启动失败：{exc}\n\n崩溃日志：{log_path}")
        return 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        try:
            log_path = _write_crash_log(_base_dir(), Exception("top-level"))
        except Exception:
            log_path = None
        _pause("启动失败（顶层异常）。"
               + (f"\n\n崩溃日志：{log_path}" if log_path else ""))
        code = 1
    sys.exit(code)
