"""`deploy doctor` — one command that answers "is this installation healthy?".

Run by the installer at the end of setup (its result is written to
``aims_data\\logs\\doctor.json`` and shown as the installer's finish-page detail), by
``tools\\Start-Aims.ps1`` when the app fails to start, and by whoever supports the
machine later. Each check returns ok / warn / fail with a one-line remedy, so the
output can be pasted into a ticket as-is.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from app.deploy import APP_VERSION, ai_engine, backup, conf, state as state_mod
from app.deploy.paths import IS_WINDOWS, Layout, load_layout
from app.deploy.pg import Postgres, port_free
from app.deploy.pgservice import exists as service_exists

OK, WARN, FAIL = "ok", "warn", "fail"


def _item(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, "可写"
    except Exception as exc:  # noqa: BLE001
        return False, f"不可写：{exc}"


def disk_free_gb(path: Path) -> float:
    try:
        usage = os.statvfs(str(path)) if hasattr(os, "statvfs") else None
        if usage:
            return round(usage.f_bavail * usage.f_frsize / 1024 ** 3, 1)
    except Exception:
        pass
    try:
        import shutil

        return round(shutil.disk_usage(str(path)).free / 1024 ** 3, 1)
    except Exception:
        return -1.0


def run(*, root: Path | None = None, api_port: int = 8000) -> dict[str, Any]:
    layout = load_layout(root)
    state = state_mod.read_state(layout)
    env = conf.load_env(layout)
    secrets = conf.read_secrets(layout)
    checks: list[dict[str, str]] = []

    # ---- installation ------------------------------------------------------
    checks.append(_item("版本", OK, f"程序 {APP_VERSION} / 已安装 {state.get('app_version') or '未知'}"
                        + (f"（{state.get('updated_at')} 更新）" if state.get("updated_at") else "")))
    missing = [str(p) for p in (layout.env_file, layout.root / "frontend.html") if not p.exists()]
    checks.append(_item("安装文件", OK if not missing else FAIL,
                        "完整" if not missing else f"缺少：{', '.join(missing)}",
                        "" if not missing else "重新运行安装程序（覆盖安装不会动数据）"))

    for label, path in (("数据目录", layout.data_dir), ("备份目录", layout.backups_dir)):
        ok, detail = _writable(path)
        checks.append(_item(label, OK if ok else FAIL, f"{path} — {detail}",
                            "" if ok else "以管理员身份运行 tools\\Repair-Aims.ps1 修复权限"))

    free = disk_free_gb(layout.data_dir)
    checks.append(_item("磁盘空间", OK if free < 0 or free >= 2 else WARN,
                        f"剩余 {free} GB" if free >= 0 else "无法检测",
                        "" if free < 0 or free >= 2 else "剩余空间不足 2 GB，备份可能失败"))

    # ---- configuration -----------------------------------------------------
    if not layout.env_file.is_file():
        checks.append(_item("配置文件", FAIL, f"缺少 {layout.env_file}",
                            "运行 AIMS-Backend.exe deploy provision"))
    else:
        url = env.get("DATABASE_URL", "")
        checks.append(_item("配置文件", OK if url.startswith("postgresql") else WARN,
                            f"{layout.env_file}（{len(env)} 项）"))
        key = env.get("SECRET_KEY", "")
        checks.append(_item("密钥", OK if len(key) >= 32 else FAIL,
                            f"SECRET_KEY 长度 {len(key)}",
                            "" if len(key) >= 32 else "重新运行 deploy provision 生成"))

    # ---- PostgreSQL --------------------------------------------------------
    # 端口以实际配置为准：安装时可能因为 55432 被占用而顺延到 55433，
    # 自检如果还去看默认值，就会把"健康的安装"报成失败。
    pg_port = conf.env_int(env, "AIMS_PG_PORT", int((state.get("pg") or {}).get("port") or 55432))
    pg = Postgres(layout, port=pg_port)
    if not pg.has_binaries():
        checks.append(_item("PostgreSQL 程序", FAIL, f"缺少 {layout.pg_bin}",
                            "安装包不完整，请重新安装"))
    else:
        text, major = pg.version()
        checks.append(_item("PostgreSQL 程序", OK, text.strip() or f"major {major}"))
        checks.append(_item("数据集群", OK if pg.initialized else FAIL,
                            f"{layout.pg_data}（{'已初始化' if pg.initialized else '未初始化'}）",
                            "" if pg.initialized else "运行 AIMS-Backend.exe deploy provision"))
        if pg.initialized:
            ready = pg.is_ready(timeout=3)
            st = pg.status()
            mode = (state.get("pg") or {}).get("mode")
            service = service_exists(pg.service) if IS_WINDOWS else False
            checks.append(_item("数据库服务", OK if ready else FAIL,
                                f"端口 {pg.port} {'可连接' if ready else '无响应'}；进程状态 {st}；"
                                f"运行模式 {mode or '未知'}；Windows 服务 {'已注册' if service else '未注册'}",
                                "" if ready else f"以管理员身份运行 tools\\Start-Aims.ps1，或查看 {layout.logs_dir}\\pgsql-startup.log"))
            if ready and secrets.get("DB_SUPERUSER_PASSWORD"):
                try:
                    ver = pg.scalar("SHOW server_version", password=secrets["DB_SUPERUSER_PASSWORD"])
                    size = pg.database_size_mb(password=secrets["DB_SUPERUSER_PASSWORD"])
                    vec = pg.scalar("SELECT count(*) FROM pg_extension WHERE extname='vector'",
                                    password=secrets["DB_SUPERUSER_PASSWORD"], database=pg.database)
                    checks.append(_item("数据库", OK, f"PostgreSQL {ver}，{pg.database} 库 {size} MB，"
                                                       f"pgvector {'已启用' if vec else '未启用'}"))
                except Exception as exc:  # noqa: BLE001
                    checks.append(_item("数据库", FAIL, f"连接失败：{exc}",
                                        "检查配置中的 DATABASE_URL 与 secrets.json 是否一致"))

    # ---- application port --------------------------------------------------
    if port_free(api_port):
        checks.append(_item("应用端口", OK, f"{api_port} 空闲（应用未运行）"))
    else:
        checks.append(_item("应用端口", WARN, f"{api_port} 已被占用 — 若 AIMS 正在运行属正常；"
                                              f"否则修改 config\\aims.env 中的 PORT"))

    # ---- AI engine ---------------------------------------------------------
    ai = ai_engine.probe(layout)
    if ai.get("backend"):
        checks.append(_item("AI 推理引擎", OK, f"llama.cpp {ai['backend']}"))
        if ai.get("models"):
            names = ", ".join(f"{m['id']}（{m['size_mb']} MB）" for m in ai["models"])
            checks.append(_item("本地模型", OK, names))
            active = ai.get("active") or {}
            checks.append(_item("激活模型", OK if active.get("active_llm") else WARN,
                                f"LLM={active.get('active_llm') or '-'}，"
                                f"Embedding={active.get('active_embedding') or '-'}",
                                "" if active.get("active_llm") else "AI Center → 模型下载 中选择一个模型"))
        else:
            checks.append(_item("本地模型", WARN, "未随安装包附带 GGUF 模型",
                                "离线环境请在构建时加 -IncludeAiModels，或在有网机器上下载后复制 cores\\models"))
    else:
        checks.append(_item("AI 推理引擎", WARN, f"本地推理不可用：{ai.get('backend_error')}",
                            "AI 中心仍可使用规则引擎；需要本地大模型时重装完整版"))

    # ---- backups -----------------------------------------------------------
    items = backup.list_backups(layout)
    if items:
        checks.append(_item("备份", OK, f"最近 {items[0]['mtime']}（{items[0]['size_bytes'] / 1e6:.1f} MB，"
                                        f"{len(items)} 份）"))
    else:
        checks.append(_item("备份", WARN, "还没有备份", "运行 tools\\Backup-Aims.ps1 立即备份一次"))

    failed = [c for c in checks if c["status"] == FAIL]
    warned = [c for c in checks if c["status"] == WARN]
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": APP_VERSION,
        "ok": not failed,
        "failed": len(failed),
        "warnings": len(warned),
        "checks": checks,
    }
    return result


def report_text(result: dict[str, Any]) -> str:
    icons = {OK: "  [OK]  ", WARN: "  [!!]  ", FAIL: "  [FAIL]"}
    lines = ["=" * 74,
             f" AIMS 安装自检  v{result['version']}   {result['generated_at']}",
             "=" * 74]
    for c in result["checks"]:
        lines.append(f"{icons.get(c['status'], '  [??]  ')}{c['check']}: {c['detail']}")
        if c["fix"]:
            lines.append(f"          → {c['fix']}")
    lines.append("-" * 74)
    if result["ok"]:
        lines.append(f" 结论：正常（{result['warnings']} 条提示）")
    else:
        lines.append(f" 结论：{result['failed']} 项失败，{result['warnings']} 项提示 — 请按上面的 → 处理")
    lines.append("=" * 74)
    return "\n".join(lines)


def write_report(layout: Layout, result: dict[str, Any]) -> Path:
    path = layout.logs_dir / "doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
