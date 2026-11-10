"""Windows service integration for the bundled PostgreSQL (and a no-op elsewhere).

Two run modes are supported, recorded in deployment.json as ``pg.mode``:

  ``service``  — the installer registered ``AIMSPostgreSQL`` via ``pg_ctl register``
                 (runs as NT AUTHORITY\\NetworkService, start type automatic). The
                 database is up before anyone logs in and survives an app crash.
                 Starting it needs elevation, so the launcher asks for it through
                 the standard UAC prompt instead of failing with a cryptic error.

  ``process``  — fallback for a per-user install (no admin rights, or service
                 registration failed): the desktop launcher starts postgres.exe
                 itself and stops it when it exits.

Both modes are transparent to the rest of the app: it only ever sees
``127.0.0.1:<port>`` with a password.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from app.deploy.paths import IS_WINDOWS
from app.deploy.pg import CREATE_NO_WINDOW, Postgres, PgError, port_open

logger = logging.getLogger("aims.deploy.service")

ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_ACCESS_DENIED = 5
ERROR_CANCELLED = 1223


def _sc(*args: str, timeout: float = 30) -> tuple[int, str]:
    """Run sc.exe and return (exit_code, combined output). Exit codes are
    language-independent, so we never parse localized status text."""
    try:
        proc = subprocess.run(
            ["sc.exe", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return -1, "sc.exe not found"
    except subprocess.TimeoutExpired:
        return -2, "sc.exe timeout"


def supported() -> bool:
    return IS_WINDOWS


def exists(name: str) -> bool:
    if not IS_WINDOWS:
        return False
    code, _ = _sc("query", name)
    return code == 0


def register(pg: Postgres, *, account: str = r"NT AUTHORITY\NetworkService",
             start_type: str = "auto") -> tuple[bool, str]:
    """Create the Windows service for this cluster (needs elevation)."""
    if not IS_WINDOWS:
        return False, "非 Windows 平台，使用进程模式"
    if exists(pg.service):
        return True, "服务已存在"
    args = [pg.binary("pg_ctl"), "-D", pg.data, "register",
            "-N", pg.service, "-S", start_type, "-U", account]
    try:
        proc = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              creationflags=CREATE_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return False, f"注册服务失败：{exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, f"注册服务失败（退出码 {proc.returncode}）：{out}"
    # Let Windows' SCM pick up the new service definition before we start it.
    time.sleep(1.0)
    return True, out or "服务已注册"


def unregister(pg: Postgres) -> tuple[bool, str]:
    if not IS_WINDOWS or not exists(pg.service):
        return True, "服务不存在"
    try:
        proc = subprocess.run([str(pg.binary("pg_ctl")), "-D", str(pg.data), "unregister", "-N", pg.service],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, creationflags=CREATE_NO_WINDOW)
        if proc.returncode != 0:
            code, out = _sc("delete", pg.service)
            return code == 0, out.strip()
        return True, (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"注销服务失败：{exc}"


def start(name: str, *, elevate: bool = True, timeout: float = 60) -> tuple[bool, str]:
    """Start the service; on access denied ask for elevation (UAC)."""
    if not IS_WINDOWS:
        return False, "非 Windows 平台"
    code, out = _sc("start", name)
    if code in (0, ERROR_SERVICE_ALREADY_RUNNING):
        return True, "服务已启动"
    if code == ERROR_ACCESS_DENIED and elevate:
        ok, msg = start_elevated(name)
        if ok:
            return True, msg
        return False, f"需要管理员权限才能启动数据库服务（{msg}）"
    return False, f"启动服务失败（退出码 {code}）：{out.strip()}"


def stop(name: str, *, elevate: bool = True) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "非 Windows 平台"
    code, out = _sc("stop", name)
    if code in (0, ERROR_SERVICE_NOT_ACTIVE):
        return True, "服务已停止"
    if code == ERROR_ACCESS_DENIED and elevate:
        ok, msg = stop_elevated(name)
        return (True, msg) if ok else (False, f"需要管理员权限才能停止数据库服务（{msg}）")
    return False, f"停止服务失败（退出码 {code}）：{out.strip()}"


def _shell_exec_elevated(exe: str, params: str) -> tuple[bool, str]:
    """ShellExecuteW(..., "runas", ...) — the UAC prompt the user expects."""
    import ctypes

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if rc <= 32:
        # 5 = access denied (UAC refused / not an admin account)
        return False, "用户取消或权限不足" if rc == 5 else f"ShellExecute 错误 {rc}"
    return True, "已请求管理员权限"


def start_elevated(name: str) -> tuple[bool, str]:
    ok, msg = _shell_exec_elevated("sc.exe", f"start {name}")
    return ok, msg


def stop_elevated(name: str) -> tuple[bool, str]:
    ok, msg = _shell_exec_elevated("sc.exe", f"stop {name}")
    return ok, msg


def ensure_running(pg: Postgres, *, mode: str, wait: float = 90.0,
                   announce: Any = None) -> bool:
    """Make sure the cluster accepts connections, whatever the run mode.

    Returns True when ready. Raises PgError with an actionable message otherwise.
    """
    say = announce or (lambda msg: None)
    if pg.is_ready():
        return True

    if mode == "service" and IS_WINDOWS and exists(pg.service):
        if not port_open(pg.host, pg.port):
            say("正在启动 PostgreSQL 服务……")
            ok, msg = start(pg.service)
            if not ok:
                logger.warning("服务启动失败：%s", msg)
                say(msg)
        if pg.wait_ready(wait):
            return True

    # Process mode, or service mode that did not come up: start pg_ctl directly.
    if pg.initialized:
        say("正在启动内置 PostgreSQL……")
        try:
            if pg.start(wait=wait):
                return True
        except PgError as exc:
            raise PgError(str(exc)) from exc

    raise PgError(
        "内置 PostgreSQL 启动失败。\n"
        f"  数据目录：{pg.data}\n"
        f"  端口：{pg.host}:{pg.port}\n"
        "请检查：1) 端口是否被其它程序占用；2) 数据目录是否可写；\n"
        "        3) 以管理员身份运行 tools\\Start-Aims.ps1 或运行 AIMS-Backend.exe deploy doctor。"
    )
