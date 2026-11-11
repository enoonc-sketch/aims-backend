"""Startup glue used by run_standalone.py in the managed (bundled PostgreSQL) install.

Order matters and is the whole point of this module:

    config/aims.env  ->  os.environ  ->  PostgreSQL ready  ->  schema reconciled
                     ->  (then, and only then) `import app.main`

The FastAPI app builds its Settings at import time, so the environment has to be
complete before that import. Doing the database work here (instead of inside the
app's lifespan) also means a broken cluster produces one clear message and a log
file, instead of a stack trace behind a window that never opens.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from app.deploy import backup, conf, pgservice, schema, state as state_mod
from app.deploy.paths import IS_WINDOWS, Layout, load_layout, managed_mode
from app.deploy.pg import Postgres, PgError

logger = logging.getLogger("aims.deploy.runtime")


class DeploymentError(RuntimeError):
    """Fatal, user-facing startup problem (message is shown in a dialog)."""


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def bootstrap(base: Path, *, announce=None) -> dict[str, Any]:
    """Prepare a managed installation. Returns a small info dict; raises
    DeploymentError when the installation cannot run."""
    say = announce or (lambda msg: None)
    layout = load_layout(base)
    info: dict[str, Any] = {"managed": False}
    if not managed_mode(layout):
        # 危险降级防护：pg_ctl 就位但数据目录不在，说明 AIMS_DATA_ROOT / AIMS_PG_DATA
        # 与安装时不一致（换了盘、改了环境变量、数据目录被移走）。此时如果继续启动，
        # 应用会回落 SQLite 并"看起来一切正常"，用户的历史数据却还在原来的 PostgreSQL 里
        # ——比直接报错危险得多，所以这里必须拦住。
        if (layout.pg_bin / layout.exe_name("pg_ctl")).is_file() and not layout.pg_data.is_dir():
            raise DeploymentError(
                f"数据库程序已就位，但数据目录不存在：{layout.pg_data}\n"
                f"当前数据根目录：{layout.base}\n"
                "这通常意味着数据目录被移动过，或 AIMS_DATA_ROOT / AIMS_PG_DATA 指向了别处。\n"
                "继续启动会以空库运行，看起来正常但历史数据仍在原目录里。\n"
                "请确认数据目录位置后重新启动；如确需以新库运行，请先执行 "
                "deploy provision 并显式指定新的数据目录。"
            )
        return info
    info["managed"] = True

    env = conf.load_env(layout)
    if not env or not env.get("DATABASE_URL", "").startswith("postgresql"):
        raise DeploymentError(
            f"配置文件缺失或不完整：{layout.env_file}\n"
            "请运行安装程序修复，或在命令行执行：AIMS-Backend.exe deploy provision"
        )
    # Real environment variables still win (support can point a run at another DB).
    conf.apply_env(env)

    state = state_mod.read_state(layout)
    mode = (state.get("pg") or {}).get("mode") or ("service" if IS_WINDOWS else "process")
    port = int(env.get("AIMS_PG_PORT") or (state.get("pg") or {}).get("port") or 55432)
    pg = Postgres(layout, port=port)
    info.update({"port": port, "mode": mode, "data_dir": str(layout.pg_data)})

    if not pg.initialized:
        raise DeploymentError(
            f"数据库集群尚未初始化：{layout.pg_data}\n"
            "请以管理员身份运行 tools\\Repair-Aims.ps1，或重新运行安装程序。"
        )

    say(f"  数据库   : PostgreSQL @ 127.0.0.1:{port}（{mode} 模式）")
    started_here = False
    if not pg.is_ready():
        say("  正在启动内置数据库……")
        try:
            pgservice.ensure_running(pg, mode=mode, announce=say)
        except PgError as exc:
            raise DeploymentError(str(exc)) from exc
        started_here = True
    info["started_here"] = started_here

    if _truthy(os.environ.get("AUTO_UPGRADE_SCHEMA"), True):
        _reconcile(layout, env, say)

    if _truthy(os.environ.get("AIMS_AUTO_BACKUP"), True):
        startup_backup(layout, pg, say)

    return info


def _reconcile(layout: Layout, env: dict[str, str], say) -> None:
    """Additive schema upgrade — see app/deploy/schema.py."""
    from sqlalchemy import create_engine

    engine = create_engine(env["DATABASE_URL"], future=True, pool_pre_ping=True)
    try:
        report = schema.reconcile(engine)
        if report["created_tables"] or report["added_columns"]:
            say(f"  结构升级 : 新增表 {len(report['created_tables'])}，新增列 {len(report['added_columns'])}")
            logger.info("schema reconciled: %s", report)
        schema.ensure_postgres_extras(engine)
    except Exception as exc:  # noqa: BLE001
        # Never block startup on a schema hiccup: init_db() in the lifespan will
        # retry, and the doctor command reports the detail.
        logger.exception("schema reconcile failed: %s", exc)
        say(f"  ! 结构检查失败（应用仍会尝试启动）：{exc}")
    finally:
        engine.dispose()


def startup_backup(layout: Layout, pg: Postgres, say) -> None:
    """At most one automatic backup per 12 h, pruned to AIMS_BACKUP_KEEP."""
    try:
        password = conf.read_secrets(layout).get("DB_SUPERUSER_PASSWORD")
        if not password:
            return
        items = backup.list_backups(layout)
        recent = [b for b in items if not b["safety"]]
        if recent:
            last = Path(recent[0]["path"]).stat().st_mtime
            if time.time() - last < 12 * 3600:
                return
        meta = backup.create_backup(pg, password=password, reason="startup", layout=layout)
        say(f"  自动备份 : {meta['file']}（{meta['size_bytes'] / 1e6:.1f} MB）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup backup skipped: %s", exc)


def shutdown(layout: Layout | None = None, *, started_here: bool = False) -> None:
    """Stop a cluster this process started itself (process mode only).

    Service-mode clusters keep running: that is the point of the service, and the
    next user to open AIMS should not pay the 3-5 s start-up cost.
    """
    layout = layout or load_layout()
    if not started_here:
        return
    try:
        pg = Postgres(layout)
        state = state_mod.read_state(layout)
        if (state.get("pg") or {}).get("mode") == "service":
            return
        if pg.status() == "running":
            pg.stop()
            logger.info("PostgreSQL stopped (started by this process)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not stop PostgreSQL: %s", exc)


# --------------------------------------------------------------------------- #
def show_error(title: str, message: str) -> None:
    """A message box on Windows (the app has no console worth reading), stderr elsewhere."""
    logger.error("%s: %s", title, message)
    if IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"\n{title}\n{message}\n", flush=True)


def diagnostic_tail(layout: Layout, lines: int = 20) -> str:
    """Last lines of the PostgreSQL log / provision log, for the error dialog."""
    chunks: list[str] = []
    for name in ("pgsql-startup.log", "provision.log"):
        path = layout.logs_dir / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").splitlines()
                chunks.append(f"--- {name} ---\n" + "\n".join(text[-lines:]))
            except Exception:
                continue
    for log in sorted(layout.pg_data.glob("pg_log/*.log")):
        try:
            text = log.read_text(encoding="utf-8", errors="replace").splitlines()
            chunks.append(f"--- {log.name} ---\n" + "\n".join(text[-lines:]))
        except Exception:
            continue
        break
    return "\n\n".join(chunks)
