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
import webbrowser
import threading
import time
import urllib.request
import ssl
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
    log_path = base / "aims_crash.log"
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
    except Exception:
        pass
    return log_path


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
        webview.create_window(
            "AIMS Backend", f"{base_url}/ui",
            width=1280, height=800,
            resizable=True, text_select=True,
            # Do not set on_top=True; pywebview 6.2.1 has an EdgeChromium
            # deadlock under activation load (r0x0r/pywebview#1823).
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

        # AIMS_OPEN_BROWSER keeps its existing meaning: "1" (default) opens
        # the UI (desktop window in a frozen EXE, browser otherwise); "0"
        # starts the server without opening anything for headless/remote use.
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # Wait until the server really answers /health (max 15 s, no proxy:
        # system proxy settings must not intercept localhost).
        healthy = False
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                handlers = [urllib.request.ProxyHandler({})]
                if scheme == "https":
                    handlers.append(urllib.request.HTTPSHandler(
                        context=ssl._create_unverified_context()))
                opener = urllib.request.build_opener(*handlers)
                req = urllib.request.Request(base_url + "/health")
                with opener.open(req, timeout=0.5) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(0.15)
        if not healthy:
            print("Server did not become ready in 15 s.")
            print(f"If it actually started, open manually: {base_url}/ui")
            server.should_exit = True
            return 1

        opened_window = False
        if os.environ.get("AIMS_OPEN_BROWSER", "1") == "1":
            opened_window = _open_ui_window(base_url, settings)

        if not opened_window:
            # With no window (source, TLS, AIMS_OPEN_BROWSER=0, or fallback),
            # wait for the server until Ctrl+C, preserving the prior behavior.
            try:
                while server_thread.is_alive():
                    server_thread.join(timeout=1)
            except KeyboardInterrupt:
                print("\nServer stopped.")
        # Closing the window ends the main thread and stops the daemon server.
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
