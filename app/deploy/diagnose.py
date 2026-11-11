"""启动失败自诊断报告。

现场反馈的典型场景：装完双击没反应/弹一个错误框，用户说不清哪里错，维护者
隔着电话只能猜。本模块把判断"为什么起不来"需要的全部事实收集成**一份文本
文件**：路径与权限、生效的配置（口令脱敏）、数据库状态、端口占用、各日志尾部。

设计约束：

* **绝不泄露口令**：只输出白名单环境变量；含 PASSWORD/SECRET/TOKEN 的值一律
  替换为 ``***``；``DATABASE_URL`` 里的密码段单独打码；``secrets.json`` 只报
  "存在/不存在"，绝不读内容。
* **绝不抛异常**：诊断代码自己失败不能把主流程带崩（每一项单独 try/except）。
* **只用标准库 + 现有 deploy 模块**：能在崩溃环境下运行，不依赖 pandas 之类。
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.deploy.paths import Layout, load_layout, managed_mode


def _width(text: str) -> int:
    """终端显示宽度：全角/宽字符算 2 列（中文字段对齐要对齐"看得见的宽度"）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _width(text))

# 只报告这些环境变量（白名单，避免把客户机器上的无关变量带出去）
_ENV_WHITELIST = (
    "AIMS_ROOT", "AIMS_DATA_ROOT", "AIMS_DATA_DIR", "AIMS_DB_MODE",
    "AIMS_PG_HOME", "AIMS_PG_BINDIR", "AIMS_PG_DATA", "AIMS_PG_PORT",
    "AIMS_MANAGED_RUNTIME", "AIMS_BACKUP_DIR", "AIMS_UPLOAD_DIR", "AIMS_MODELS_DIR",
    "APP_ENV", "HOST", "PORT", "DATABASE_URL", "AUTO_INIT_DB", "AUTO_UPGRADE_SCHEMA",
    "AIMS_LOG_FORMAT", "AIMS_LOG_LEVEL", "AIMS_LOG_FILE", "AIMS_OPEN_BROWSER",
    "AIMS_DEBUG_CONSOLE", "AIMS_PARENT_PID",
)
_SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "PRIVATE_KEY")
_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")


def mask_url(url: str) -> str:
    """把 DATABASE_URL 里的密码打码：``postgresql://aims:123456@host/db``。"""
    if not url:
        return url
    return _URL_PASSWORD.sub(lambda m: m.group(1) + "***" + m.group(3), url)


def _mask_value(key: str, value: str) -> str:
    if not value:
        return value
    if key == "DATABASE_URL":
        return mask_url(value)
    if any(word in key.upper() for word in _SECRET_WORDS):
        return "***（已脱敏，长度 %d）" % len(value)
    return value


def env_snapshot() -> dict[str, str]:
    snap: dict[str, str] = {}
    for key in _ENV_WHITELIST:
        if key in os.environ:
            snap[key] = _mask_value(key, os.environ.get(key, ""))
    return snap


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def _writable(path: Path) -> str:
    """目录是否可写（用真实建文件判断；只读介质/权限不足都在这里暴露）。"""
    try:
        if not path.exists():
            return "目录不存在"
        probe = path / f".aims-write-test-{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return "可写"
    except PermissionError:
        return "无写权限"
    except OSError as exc:
        return f"不可写（{exc.__class__.__name__}）"


def _port_state(host: str, port: int) -> str:
    """端口是否已被占用（能连上 = 有人监听）。"""
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return "已占用（有服务在监听）"
    except OSError:
        return "空闲"
    except Exception:
        return "未知"


def _tail(path: Path, lines: int = 40, limit: int = 6000) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = "\n".join(text[-lines:])
        return body[-limit:]
    except Exception:
        return ""


def _crash_log_candidates(layout: Layout) -> list[Path]:
    seen: list[Path] = []
    for p in (layout.root / "aims_crash.log",
              layout.base / "aims_crash.log",
              layout.data_dir / "aims_crash.log",
              Path(tempfile.gettempdir()) / "aims_crash.log"):
        if p not in seen:
            seen.append(p)
    return seen


def _database_section(layout: Layout) -> dict[str, Any]:
    info: dict[str, Any] = {"managed": False}
    try:
        info["managed"] = managed_mode(layout)
    except Exception as exc:
        info["managed_error"] = str(exc)
    info["pg_bin"] = str(layout.pg_bin)
    info["pg_ctl_exists"] = (layout.pg_bin / layout.exe_name("pg_ctl")).is_file()
    info["data_dir"] = str(layout.pg_data)
    info["data_dir_exists"] = layout.pg_data.is_dir()
    info["initialized"] = (layout.pg_data / "PG_VERSION").is_file()
    try:
        info["state"] = json.loads(layout.state_file.read_text(encoding="utf-8"))
    except Exception:
        info["state"] = None
    try:
        from app.deploy.conf import load_env

        env = load_env(layout)
        info["env_file_exists"] = layout.env_file.is_file()
        info["env_keys"] = sorted(env.keys())
        if env:
            info["pg_port"] = env.get("AIMS_PG_PORT")
            info["database_url"] = mask_url(env.get("DATABASE_URL", ""))
    except Exception as exc:
        info["env_error"] = str(exc)
    # 集群是否真的在跑（有 postmaster.pid 才算"我们的"集群）
    try:
        from app.deploy.pg import Postgres

        port = int(os.environ.get("AIMS_PG_PORT") or (info.get("state") or {}).get("pg", {}).get("port") or 55432)
        pg = Postgres(layout, port=port)
        info["pg_port_effective"] = port
        info["postmaster_pid"] = pg.running_pid()
        info["ready"] = pg.is_ready()
        info["port_state"] = _port_state("127.0.0.1", port)
    except Exception as exc:
        info["pg_error"] = str(exc)
    return info


def collect(base: Path | None = None, *, reason: str | None = None) -> dict[str, Any]:
    layout = load_layout(base)
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "",
        "version": "",
        "runtime": {},
        "paths": {},
        "env": {},
        "database": {},
        "logs": {},
    }
    try:
        from app.deploy import APP_VERSION

        report["version"] = APP_VERSION
    except Exception:
        report["version"] = "unknown"

    try:
        report["runtime"] = {
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "meipass": getattr(sys, "_MEIPASS", ""),
            "cwd": os.getcwd(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
            "user": (os.environ.get("USERNAME") or os.environ.get("USER")
                     or _current_user()),
            "argv": sys.argv,
        }
    except Exception:
        pass

    try:
        paths = layout.describe()
        extra = {}
        for label, p in (("程序目录", layout.root), ("数据根", layout.base),
                         ("配置目录", layout.config_dir), ("日志目录", layout.logs_dir),
                         ("数据库数据目录", layout.pg_data), ("备份目录", layout.backups_dir)):
            extra[label] = {"路径": str(p), "存在": p.exists(), "可写": _writable(p)}
        report["paths"] = {"概览": paths, "明细": extra}
    except Exception as exc:
        report["paths"] = {"error": str(exc)}

    try:
        report["env"] = env_snapshot()
    except Exception:
        pass

    try:
        report["database"] = _database_section(layout)
    except Exception as exc:
        report["database"] = {"error": str(exc)}

    # 日志尾部：API 日志、PG 启动日志、provision 日志、安装后体检、崩溃日志
    logs: dict[str, str] = {}
    for label, path in (
        ("aims.log（产品安装）", layout.logs_dir / "aims.log"),
        ("aims.log（绿色运行）", layout.data_dir / "logs" / "aims.log"),
        ("pgsql-startup.log", layout.logs_dir / "pgsql-startup.log"),
        ("provision.log", layout.logs_dir / "provision.log"),
        ("post-install-doctor.json", layout.logs_dir / "post-install-doctor.json"),
    ):
        body = _tail(path)
        if body:
            logs[label] = body
    for candidate in _crash_log_candidates(layout):
        body = _tail(candidate)
        if body:
            logs[f"崩溃日志：{candidate}"] = body
            break
    for pg_log in sorted(layout.pg_data.glob("pg_log/*.log"))[-1:]:
        body = _tail(pg_log)
        if body:
            logs[f"数据库日志：{pg_log.name}"] = body
    report["logs"] = logs

    # 端口：API 服务端口是否被别的进程占着（最常见的"起不来"原因之一）
    try:
        port = int(os.environ.get("PORT") or 8000)
        report["runtime"]["api_port"] = port
        report["runtime"]["api_port_state"] = _port_state("127.0.0.1", port)
    except Exception:
        pass
    return report


def verdicts(report: dict[str, Any]) -> list[str]:
    """把采集到的事实翻译成结论。

    用户看不懂"postmaster.pid 不存在"，但看得懂"端口 55432 被别的程序占了"。
    这里只做基于事实的确定性判断，不做猜测——不确定就不说。
    """
    out: list[str] = []
    db = report.get("database") or {}
    rt = report.get("runtime") or {}
    paths = (report.get("paths") or {}).get("明细") or {}

    if db.get("pg_ctl_exists") and not db.get("initialized"):
        out.append(
            f"数据库程序已就位，但集群未初始化或数据目录不在预期位置：{db.get('data_dir')}。"
            "这通常是安装未完成，或数据目录被移动过（Config\\aims.env 的 AIMS_PG_DATA "
            "与实际位置不一致）。"
        )
    if str(db.get("port_state", "")).startswith("已占用") and not db.get("postmaster_pid"):
        out.append(
            f"数据库端口 {db.get('pg_port_effective')} 已被占用，但占用者不是 AIMS 自己的集群"
            "（没有 postmaster.pid）——通常是机器上另一个 PostgreSQL，或上次异常退出残留的进程。"
        )
    if db.get("postmaster_pid") and db.get("ready") is False:
        out.append(
            "数据库进程在跑，但不接受连接：看下面数据库日志的尾部。常见原因：磁盘满、"
            "端口被抢、上次崩溃后还在恢复。"
        )
    if str(rt.get("api_port_state", "")).startswith("已占用"):
        out.append(
            f"API 端口 {rt.get('api_port')} 已被占用：关掉占用该端口的程序，"
            "或在 Config\\aims.env 里改 PORT。"
        )
    for label in ("配置目录", "日志目录", "数据根"):
        item = paths.get(label) or {}
        if item and item.get("存在") is False:
            out.append(f"{label}不存在：{item.get('路径')}（安装不完整，或数据根被移动过）")
        elif item and item.get("可写") not in (None, "可写"):
            out.append(f"{label}{item.get('可写')}：{item.get('路径')}（权限被策略改动过？）")
    if not (report.get("logs") or {}):
        out.append("没有任何日志：程序可能从未成功启动过，先看安装阶段的 Logs\\post-install-doctor.json。")
    return out


def render(report: dict[str, Any]) -> str:
    """把采集结果渲染成给人读的文本（顺序即排查顺序）。"""
    out: list[str] = []
    add = out.append
    add("=" * 72)
    add("  AIMS 启动自诊断报告")
    add("=" * 72)
    add(f"生成时间 : {report.get('generated_at', '')}")
    add(f"版本     : {report.get('version', '')}")
    if report.get("reason"):
        add(f"失败现象 : {report['reason']}")
    add("")

    findings = verdicts(report)
    if findings:
        add("-- 关键判断 " + "-" * 60)
        for i, item in enumerate(findings, 1):
            add(f"{i}. {item}")
        add("")

    rt = report.get("runtime") or {}
    if rt:
        add("-- 运行环境 " + "-" * 58)
        for key, label in (("executable", "程序"), ("frozen", "打包运行"),
                           ("cwd", "工作目录"), ("python", "Python"),
                           ("platform", "操作系统"), ("pid", "进程号"),
                           ("user", "用户"), ("api_port", "API 端口"),
                           ("api_port_state", "API 端口状态")):
            if key in rt:
                add(f"{_pad(label, 13)}: {rt[key]}")
        if rt.get("argv") is not None:
            add(f"{_pad('启动参数', 13)}: {rt['argv']}")
        add("")

    paths = report.get("paths") or {}
    if paths.get("明细"):
        add("-- 路径与权限 " + "-" * 58)
        add(_pad("位置", 18) + _pad("存在", 8) + _pad("可写", 14) + "路径")
        for label, item in paths["明细"].items():
            add(_pad(label, 18) + _pad(str(item["存在"]), 8)
                + _pad(str(item["可写"]), 14) + item["路径"])
        add("")
    elif paths.get("error"):
        add(f"-- 路径信息获取失败：{paths['error']}\n")

    env = report.get("env") or {}
    if env:
        add("-- 生效配置（口令已脱敏） " + "-" * 44)
        for key in sorted(env):
            add(f"{key} = {env[key]}")
        add("")

    db = report.get("database") or {}
    if db:
        add("-- 数据库 " + "-" * 62)
        add(_pad("托管模式", 13) + ": " + str(db.get("managed")))
        add(_pad("pg_ctl 存在", 13) + ": " + str(db.get("pg_ctl_exists")))
        add(_pad("数据目录", 13) + f": {db.get('data_dir')}"
            f"（存在 {db.get('data_dir_exists')}，已初始化 {db.get('initialized')}）")
        if "pg_port_effective" in db:
            add(_pad("数据库端口", 13) + f": {db['pg_port_effective']}（{db.get('port_state')}）")
        add(_pad("postmaster", 13) + ": " + str(db.get("postmaster_pid") or "未运行"))
        add(_pad("就绪", 13) + ": " + str(db.get("ready")))
        if db.get("env_file_exists") is not None:
            add(_pad("配置文件", 13) + f": {report.get('paths', {}).get('概览', {}).get('配置')}"
                f"（存在 {db['env_file_exists']}）")
        if db.get("database_url"):
            add(_pad("连接串", 13) + ": " + str(db["database_url"]))
        for key in ("env_error", "pg_error", "managed_error"):
            if db.get(key):
                add(f"{key}: {db[key]}")
        add("")

    logs = report.get("logs") or {}
    if logs:
        add("-- 日志尾部 " + "-" * 60)
        for label, body in logs.items():
            add(f"\n### {label}")
            add(body)
        add("")
    else:
        add("-- 未找到任何日志（程序可能还没跑起来过）\n")

    add("=" * 72)
    add("提示：把这份文件完整发给维护者即可定位；其中不含任何口令明文。")
    add("=" * 72)
    return "\n".join(out)


def write_report(base: Path | None = None, *, reason: str | None = None) -> Path | None:
    """生成并落盘报告，返回路径；任何失败都返回 None（不得影响主流程）。"""
    try:
        layout = load_layout(base)
        text = render(collect(base, reason=reason))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for directory in (layout.logs_dir, layout.data_dir,
                          Path(tempfile.gettempdir())):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"aims-diagnose-{stamp}.txt"
                target.write_text(text, encoding="utf-8")
                # 固定名副本：用户/客服只要认一个文件名
                latest = directory / "aims-diagnose-latest.txt"
                try:
                    latest.write_text(text, encoding="utf-8")
                except OSError:
                    pass
                return target
            except OSError:
                continue
    except Exception:
        return None
    return None
