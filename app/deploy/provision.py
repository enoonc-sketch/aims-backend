"""`deploy provision` — the single command the installer runs, and the same code
the desktop launcher uses to repair/upgrade itself.

Steps (each idempotent, each with a clear log line so a failed install on a
customer machine is diagnosable from ``aims_data\\logs\\provision.log``):

  1. resolve layout, generate secrets (once — never regenerated on upgrade)
  2. initdb the bundled cluster (if not already initialised)
  3. write aims.conf / pg_hba.conf (sizing derived from the machine's RAM)
  4. register + start the Windows service (fallback: process mode)
  5. create the app role, database and extensions (vector / pg_trgm)
  6. write config/aims.env (DATABASE_URL, JWT key, bootstrap admin, AI settings)
  7. reconcile the schema, seed roles + first administrator
  8. register bundled GGUF models with the AI engine + active.json
  9. take a post-install backup and record everything in deployment.json
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from app.deploy import APP_VERSION, ai_engine, backup, conf, pgservice, schema, state as state_mod
from app.deploy.paths import IS_WINDOWS, Layout, load_layout, managed_mode
from app.deploy.pg import Postgres, PgError, port_free

logger = logging.getLogger("aims.deploy.provision")

FIRST_PG_PORT = 55432
LAST_PG_PORT = 55500


class ProvisionError(RuntimeError):
    pass


def _say(printer: Callable[[str], None] | None = None):
    out = printer or (lambda m: print(m, flush=True))

    def say(msg: str = "") -> None:
        out(msg)
        logger.info(msg)

    return say


def pick_port(state: dict, *, preferred: int | None = None, host: str = "127.0.0.1",
              pg: Postgres | None = None) -> int:
    """Stable port: keep the recorded one, otherwise the first free port in range."""
    recorded = int((state.get("pg") or {}).get("port") or 0)
    if preferred:
        return preferred
    if recorded:
        if (pg is not None and pg.is_ready()) or port_free(recorded, host):
            return recorded
        logger.warning("记录的端口 %s 已被占用，重新选择", recorded)
    for port in range(FIRST_PG_PORT, LAST_PG_PORT):
        if port_free(port, host):
            return port
    raise ProvisionError(f"{FIRST_PG_PORT}-{LAST_PG_PORT} 范围内没有可用端口，请手动指定 --pg-port")


def _grant_service_acl(layout: Layout) -> None:
    """Windows: let the PostgreSQL service account use the data directory.

    LocalSystem keeps its implicit access; NETWORK SERVICE (S-1-5-20) and
    LOCAL SERVICE (S-1-5-19) need explicit rights, and we address them by SID so
    the command works on a Chinese/English/any-locale Windows.
    """
    if not IS_WINDOWS:
        return
    targets = [layout.pg_data, layout.backups_dir, layout.logs_dir, layout.data_dir]
    for target in targets:
        if not target.exists():
            continue
        for sid in ("*S-1-5-20", "*S-1-5-19"):
            try:
                subprocess.run(["icacls", str(target), "/grant", f"{sid}:(OI)(CI)F", "/T", "/Q"],
                               capture_output=True, text=True, timeout=300,
                               creationflags=0x08000000)
            except Exception as exc:  # noqa: BLE001
                logger.warning("icacls 授权失败 %s %s: %s", target, sid, exc)


def _service_mode_requested(mode: str) -> bool:
    if mode == "auto":
        return IS_WINDOWS
    return mode == "service"


def provision(*, root: Path | None = None, mode: str = "auto", pg_port: int | None = None,
              api_port: int = 8000, admin_username: str = "admin", admin_password: str | None = None,
              seed: bool = True, verify_ai: bool = False, keep_config: bool = False,
              with_service: bool = True, printer: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Provision or repair a local installation. Returns a JSON-able report."""
    say = _say(printer)
    layout = load_layout(root)
    layout.ensure_dirs()
    started = time.time()
    report: dict[str, Any] = {"version": APP_VERSION, "root": str(layout.root), "steps": []}

    def step(name: str, **extra: Any) -> None:
        report["steps"].append({"step": name, **extra})
        say(f"  [{name}] " + (" ".join(f"{k}={v}" for k, v in extra.items()) if extra else "ok"))

    say("=" * 66)
    say(f" AIMS 本地部署初始化  v{APP_VERSION}")
    say(f" 安装目录: {layout.root}")
    say("=" * 66)

    # 1 ---- secrets ---------------------------------------------------------
    secrets = conf.ensure_secrets(layout)
    if admin_password:
        secrets["ADMIN_PASSWORD"] = admin_password
        conf._write_private(conf.secrets_path(layout), secrets)
    step("secrets", file=str(conf.secrets_path(layout)))

    # 2 ---- cluster ---------------------------------------------------------
    st = state_mod.read_state(layout)
    pg_state = st.setdefault("pg", {})
    pg = Postgres(layout, port=pg_port or int(pg_state.get("port") or 0) or FIRST_PG_PORT)
    if not pg.has_binaries():
        raise ProvisionError(f"未找到内置 PostgreSQL 程序：{layout.pg_bin}\n安装包不完整，请重新安装。")
    if not pg_port and not (pg.initialized and pg.is_ready()):
        pg.port = pick_port(st, pg=pg)

    version_text, major = pg.version()
    step("postgres", version=version_text.split(") ")[-1], major=major, bin=str(layout.pg_bin))

    if not pg.initialized:
        say(f"  正在初始化数据库集群（{layout.pg_data}）……")
        pg.initdb(secrets["DB_SUPERUSER_PASSWORD"])
        step("initdb", data=str(layout.pg_data))
    else:
        pg.configure()
        step("cluster", reused=True, data=str(layout.pg_data), port=pg.port)

    # 3 ---- service / start -------------------------------------------------
    wanted_service = with_service and _service_mode_requested(mode)
    run_mode = "process"
    if wanted_service:
        _grant_service_acl(layout)
        ok, msg = pgservice.register(pg)
        if ok:
            run_mode = "service"
            say(f"  Windows 服务 {pg.service} 已注册：{msg.strip()[:120]}")
            pg_status = pg.status()
            if pg_status == "running":
                # A service-managed cluster is already up; nothing to do.
                pass
            started_ok, smsg = pgservice.start(pg.service)
            if not started_ok:
                say(f"  服务启动失败，回退到进程模式：{smsg}")
                run_mode = "process"
        else:
            say(f"  服务注册失败，回退到进程模式：{msg}")

    if run_mode == "process" or not pg.is_ready():
        try:
            pg.start()
        except PgError as exc:
            raise ProvisionError(
                f"{exc}\n如果数据目录属于其它 Windows 账户，请以管理员身份运行 tools\\Start-Aims.ps1。"
            ) from exc
    if not pg.wait_ready(120):
        raise ProvisionError("数据库未能在 120 秒内就绪，请查看 aims_data\\logs\\pgsql-startup.log")
    step("start", mode=run_mode, port=pg.port, ready=True)

    # 4 ---- database / role / extensions -----------------------------------
    ext = pg.ensure_database(superuser_password=secrets["DB_SUPERUSER_PASSWORD"],
                             app_password=secrets["DB_PASSWORD"])
    step("database", db=pg.database, role=pg.app_user,
         created_db=ext.get("created_db"), created_role=ext.get("created_role"),
         extensions=",".join(ext.get("extensions") or []) or "-",
         vector=ext.get("vector"), timescale=ext.get("timescale"))

    # 5 ---- configuration ---------------------------------------------------
    pg_state.update({"major": major, "port": pg.port, "host": pg.host, "data_dir": str(pg.data),
                     "service": pg.service, "mode": run_mode,
                     "vector": bool(ext.get("vector")), "timescale": bool(ext.get("timescale"))})
    st["pg"] = pg_state
    st["paths"] = {"root": str(layout.root), "data": str(layout.data_dir),
                   "backups": str(layout.backups_dir), "models": str(layout.models_dir)}

    env_values = conf.build_env(layout, st, secrets, port=pg.port, api_port=api_port,
                                admin_username=admin_username)
    # The vector column is created with AI_EMBEDDING_DIM; it must match the bundled
    # embedding model, or knowledge chunks fail to store.
    env_values["AI_EMBEDDING_DIM"] = str(ai_engine.DEFAULT_EMBEDDING_DIM)
    env_values["AI_LOCAL_LLM_THREADS"] = str(ai_engine.recommended_threads())
    if not ext.get("vector"):
        env_values["EMBEDDING_STORAGE"] = "json"
    if keep_config and layout.env_file.is_file():
        parsed = conf.parse_env_file(layout.env_file)
        parsed.update({k: v for k, v in env_values.items() if k in ("HOST", "PORT")})
        env_values = {**env_values, **{k: v for k, v in parsed.items() if k not in env_values or k in ("HOST", "PORT")}}
    conf.write_env(layout, env_values)
    step("config", file=str(layout.env_file))

    # Apply now for the rest of this process (schema + seeding below).
    _apply_env(env_values)

    # 6 ---- schema + seed ---------------------------------------------------
    from sqlalchemy import create_engine

    from app.db.base import Base  # noqa: F401  (ensures models are importable)

    engine = create_engine(env_values["DATABASE_URL"], future=True, pool_pre_ping=True)
    try:
        schema_report = schema.reconcile(engine)
        extras = schema.ensure_postgres_extras(engine)
        step("schema", created_tables=len(schema_report["created_tables"]),
             added_columns=len(schema_report["added_columns"]),
             added_indexes=len(schema_report["added_indexes"]),
             hypertable=extras.get("hypertable"), vector_index=extras.get("vector_index"),
             brin=extras.get("brin_index"))
        for warn in schema_report["warnings"][:8]:
            say(f"    ! {warn}")

        from app.db.init_db import init_db as seed_db, BOOTSTRAP_ADMIN_CREATED

        seed_db(seed=seed)
        step("seed", demo_data=seed)
    finally:
        engine.dispose()

    # 6b ---- 首次登录凭据 ----------------------------------------------------
    # 首个管理员是 provision 随机生成口令创建的（secrets.json 里的 JSON 人读不了），
    # 必须同步给用户一份可读凭据，否则用户只能拿 admin/admin 反复试错。
    if BOOTSTRAP_ADMIN_CREATED:
        creds_file = conf.write_first_login_credentials(
            layout, username=admin_username, password=secrets["ADMIN_PASSWORD"],
            api_port=api_port)
        report["admin_credentials_file"] = str(creds_file)
        say(f"  [admin] 首次登录凭据已写入：{creds_file}")
        say("          （用户名与初始密码见该文件；首次登录后请立即修改密码）")

    # 7 ---- AI engine -------------------------------------------------------
    # 内置模型随安装包放在只读的程序目录；先落到可写模型目录，再做登记，
    # 否则数据库里登记的是一个普通用户再也读不到的路径（升级后尤其容易踩）。
    staged = ai_engine.sync_bundled_models(layout, announce=say)
    if staged:
        step("models", copied=len(staged))
    registry = ai_engine.ensure_registry(layout)
    registered = ai_engine.register_in_db(calculate_hash=True)
    ai_probe = ai_engine.probe(layout, deep=verify_ai)
    step("ai_engine", backend=ai_probe.get("backend") or "未安装 llama.cpp",
         models=len(ai_probe.get("models") or []), registered=registered,
         active=",".join(f"{k}={v}" for k, v in (registry or {}).items()) or "-")
    if verify_ai and ai_probe.get("warmup") is not None:
        warm = ai_probe["warmup"]
        say(f"    AI 自检: {'通过' if warm.get('ok') else '未通过'} {warm.get('reason') or warm.get('dim') or ''}")
        if not warm.get("ok"):
            # Not fatal: the app still runs with the rule engine, but the report
            # must say so — that is exactly the silent-failure this replaces.
            report["warnings"] = report.get("warnings", []) + [f"AI 自检未通过：{warm.get('reason')}"]

    # 8 ---- backup + state --------------------------------------------------
    try:
        meta = backup.create_backup(pg, password=secrets["DB_SUPERUSER_PASSWORD"],
                                    reason="post-install" if not st.get("installed_at") else "post-upgrade",
                                    layout=layout)
        st["last_backup"] = meta
        step("backup", file=meta["file"], size_mb=round(meta["size_bytes"] / 1e6, 1))
    except PgError as exc:
        say(f"    ! 初始备份失败（不影响使用）：{exc}")

    action = "install" if not st.get("installed_at") else "upgrade"
    state_mod.record_version(st, APP_VERSION, action, note=f"provision ({run_mode})")
    st["last_schema"] = {
        "created_tables": schema_report["created_tables"],
        "added_columns": schema_report["added_columns"][:50],
        "warnings": schema_report["warnings"][:20],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    state_mod.write_state(st, layout)
    step("state", file=str(layout.state_file), action=action)

    # 9 ---- mirror into system_settings (visible in the app) ----------------
    _mirror_settings(st, env_values)
    _write_report(layout, {**report, "duration_s": round(time.time() - started, 1)})

    say("-" * 66)
    say(f" 完成：{action} 完成，用时 {report.get('duration_s') or round(time.time() - started, 1)} 秒")
    say(f" 数据库：127.0.0.1:{pg.port}/{pg.database}  模式：{run_mode}")
    say(f" 配置文件：{layout.env_file}")
    say("-" * 66)
    report["duration_s"] = round(time.time() - started, 1)
    report["pg"] = {"port": pg.port, "mode": run_mode, "major": major,
                    "vector": bool(ext.get("vector"))}
    report["ok"] = True
    return report


def _apply_env(values: dict[str, str]) -> None:
    """Set os.environ *and* refresh the already-imported Settings singleton."""
    conf.apply_env(values, override=True)
    try:
        import app.core.config as config_mod

        settings = config_mod.reload_settings()
        config_mod.settings = settings
        # Model modules import `settings` at module import time; refresh the
        # embedding type decision for the current process.
        for mod_name in [m for m in list(sys.modules) if m.startswith("app.models")]:
            mod = sys.modules[mod_name]
            if hasattr(mod, "settings"):
                mod.settings = settings  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings refresh skipped: %s", exc)


def _mirror_settings(st: dict, env_values: dict[str, str]) -> None:
    """Keep a copy of the deployment info inside the app's own settings table."""
    try:
        from app.db.session import get_session_factory
        from app.models.system import SystemSetting

        session = get_session_factory()()
        try:
            info = {
                "version": st.get("app_version"),
                "build_id": st.get("build_id"),
                "installed_at": st.get("installed_at"),
                "updated_at": st.get("updated_at"),
                "mode": (st.get("pg") or {}).get("mode"),
                "pg_port": (st.get("pg") or {}).get("port"),
                "data_dir": (st.get("paths") or {}).get("data"),
                "backup_dir": (st.get("paths") or {}).get("backups"),
                "history": st.get("history", [])[-5:],
            }
            row = session.query(SystemSetting).filter(SystemSetting.key == "deployment.info").one_or_none()
            if row is None:
                row = SystemSetting(key="deployment.info", description="本地部署信息（安装程序写入，只读）")
                session.add(row)
            row.value = json.dumps(info, ensure_ascii=False)
            session.commit()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 system_settings 失败：%s", exc)


def _write_report(layout: Layout, report: dict) -> None:
    try:
        path = layout.logs_dir / "provision.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
