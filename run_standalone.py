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
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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


def _pause(msg: str = "Press Enter to exit...") -> None:
    try:
        if sys.stdin and sys.stdin.isatty():
            input(msg)
        else:
            if os.name == "nt":
                os.system("pause")
            else:
                input(msg)
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
    base = _base_dir()
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
    print(f"  Database : {os.environ.get('DATABASE_URL')}")
    print("=" * 60)
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
                _pause()
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
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

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
            if not server_thread.is_alive():
                print(f"The server stopped right after starting. Most likely port {port} is already in use "
                      "by another program: set PORT in .env or free the port. See the messages above.")
            else:
                print("Server did not become ready in 15 s.")
                print(f"If it actually started, open manually: {base_url}/ui")
            server.should_exit = True
            if getattr(sys, "frozen", False):
                _pause()  # keep the console open so a double-click user can read the message
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
        _pause()
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
        _pause()
        return 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        try:
            _write_crash_log(_base_dir(), Exception("top-level"))
        except Exception:
            pass
        _pause()
        code = 1
    sys.exit(code)
