"""服务编排：数据库 → AI 引擎 → 后端 → 主界面，按依赖顺序启动并按逆序停止。

设计要点（每一条都对应一类真实的现场故障）：

* **进程隔离**：后端是独立子进程，不是启动器里的一个线程。后端崩了启动器还活着，
  能重启它、能弹窗、能写日志；反过来用户关启动器时，我们明确地把它收掉。
* **只启动一次**：先探测 /health，已经在跑就直接开窗口，绝不"再起一个"。
* **就绪 ≠ 端口打开就绪**：数据库要 `SELECT 1` 通过、后端要 /health 返回 200，
  中间用退避重试，超时就带着日志尾巴报错，而不是假装成功。
* **不留孤儿**：Windows 上用创建进程组 + taskkill /T 收掉整棵树；
  数据库只在"本次启动自己拉起来的"且非服务模式时才关闭。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.deploy import ai_engine, conf, pgservice
from app.deploy import state as state_mod
from app.deploy.paths import IS_WINDOWS, Layout, load_layout, managed_mode
from app.deploy.pg import PgError, Postgres, port_free, port_open

logger = logging.getLogger("aims.launcher.supervisor")

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


def _http_json(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    return _http_json(f"http://127.0.0.1:{port}/health", timeout=timeout)


@dataclass
class ServiceState:
    name: str
    state: str = "stopped"          # stopped | starting | running | failed
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "detail": self.detail,
                "checked_at": round(self.checked_at, 3)}


class SupervisorError(RuntimeError):
    """启动失败，消息是给最终用户看的中文说明。"""


class Supervisor:
    """一个安装实例的运行时编排器（进程内单例）。"""

    def __init__(self, root: Path | None = None, *, progress: Progress | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else _default_root()
        os.environ["AIMS_ROOT"] = str(self.root)
        self.layout: Layout = load_layout(self.root)
        self.progress: Progress = progress or _noop
        self.env: dict[str, str] = {}
        self.api_port = 8000
        self.pg: Postgres | None = None
        self.backend: subprocess.Popen | None = None
        self.started_db_here = False
        self.started_backend_here = False
        self._stop_lock = threading.Lock()
        self._stopping = False
        # 托盘读取“界面登录状态”用的口令：随机生成，只经环境变量交给本次拉起的后端
        self.launcher_token = secrets.token_urlsafe(24)
        self.state = {
            "database": ServiceState("database"),
            "ai": ServiceState("ai"),
            "backend": ServiceState("backend"),
            "ui": ServiceState("ui"),
        }

    # ------------------------------------------------------------------ 配置
    def load(self) -> None:
        self.layout.ensure_dirs()
        self.env = conf.load_env(self.layout)
        if not self.env.get("DATABASE_URL", "").startswith("postgresql") and managed_mode(self.layout):
            raise SupervisorError(
                f"配置文件缺失或不完整：{self.layout.env_file}\n\n"
                "请重新运行安装程序，或执行：\n    AIMS-Backend.exe deploy provision --keep-config"
            )
        self.api_port = int(self.env.get("PORT", 8000) or 8000)
        st = state_mod.read_state(self.layout)
        pg_port = int(self.env.get("AIMS_PG_PORT") or (st.get("pg") or {}).get("port") or 55432)
        self.pg = Postgres(self.layout, port=pg_port)
        logger.info("supervisor root=%s api_port=%s pg_port=%s managed=%s",
                    self.root, self.api_port, pg_port, managed_mode(self.layout))

    # --------------------------------------------------------------- 数据库
    def start_database(self) -> None:
        svc = self.state["database"]
        svc.state = "starting"
        pg = self.pg
        assert pg is not None
        if not pg.has_binaries():
            svc.state = "failed"
            svc.detail = f"缺少内置数据库程序：{self.layout.pg_bin}"
            raise SupervisorError(
                f"安装不完整：找不到 {self.layout.pg_bin}\\pg_ctl\n\n"
                "请重新运行 AIMS 安装程序（覆盖安装不会影响现有数据）。"
            )
        if not pg.initialized:
            svc.state = "failed"
            svc.detail = f"数据集群未初始化：{self.layout.pg_data}"
            raise SupervisorError(
                "数据库尚未初始化。\n\n请以管理员身份运行开始菜单里的「AIMS 修复安装」，"
                "或重新运行安装程序。"
            )
        if pg.is_ready(timeout=1.5):
            svc.state, svc.detail = "running", "已在运行"
            self.progress("  数据库   : 已运行")
            return
        self.progress("  数据库   : 正在启动内置 PostgreSQL ……")
        try:
            ok = pg.start(wait=120)
        except PgError as exc:
            svc.state, svc.detail = "failed", str(exc)
            raise SupervisorError(str(exc)) from exc
        if not ok or not pg.is_ready(timeout=15):
            detail = _tail(self.layout.logs_dir / "pgsql-startup.log")
            svc.state, svc.detail = "failed", detail or "启动超时"
            raise SupervisorError(
                f"内置数据库启动失败（端口 {pg.port}）。\n\n{detail}\n\n"
                "常见原因：端口被占用、数据目录被其它账户占用、磁盘空间不足。\n"
                "打开「AIMS 诊断报告」可看到完整日志。"
            )
        self.started_db_here = True
        svc.state, svc.detail = "running", f"127.0.0.1:{pg.port}"
        self.progress(f"  数据库   : 就绪（127.0.0.1:{pg.port}）")

    # ---------------------------------------------------------------- AI 引擎
    def start_ai(self) -> None:
        """AI 引擎是后端进程内的 llama.cpp，这里做"部署前检查 + 就绪后自检"。

        不单独拉起一个 AI 守护进程，是因为：模型常驻内存，独立进程会和后端争
        RAM，退出时更容易漏；而后端本身就是唯一会调用模型的进程。
        """
        svc = self.state["ai"]
        svc.state = "starting"
        try:
            # 升级覆盖程序目录后，新版本自带的模型要先落到可写模型目录；
            # 已存在且大小一致时是零成本的空操作。
            ai_engine.sync_bundled_models(self.layout, announce=self.progress)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model sync skipped: %s", exc)
        try:
            probe = ai_engine.probe(self.layout, deep=False)
        except Exception as exc:  # noqa: BLE001
            svc.state, svc.detail = "failed", str(exc)
            self.progress(f"  AI 引擎  : 检查失败（不影响启动）：{exc}")
            return
        models = probe.get("models") or []
        svc.detail = f"backend={probe.get('backend') or '未安装'} 模型={len(models)}"
        if probe.get("backend"):
            svc.state = "running"
            self.progress(f"  AI 引擎  : {probe.get('backend')}（模型 {len(models)} 个）")
            # 启动时对 embedding 模型做一次真实推理预热（总方案 10.3）：
            # DLL 缺失 / CPU 指令集不支持 / 模型文件损坏只有真实加载才暴露，
            # 不能等到用户第一次问答才发现。失败不阻断启动，如实降级。
            try:
                warm = ai_engine.warmup(self.layout, kind="embedding")
                if not warm.get("ok"):
                    svc.detail += "；预热未通过：" + str(warm.get("reason", ""))
                    self.progress(f"  AI 引擎  : 模型预热未通过（{warm.get('reason')}），首次使用时可能再次失败")
            except Exception as exc:  # noqa: BLE001
                logger.warning("ai warmup skipped: %s", exc)
        else:
            # 规则引擎仍然可用，这是"降级"不是"失败"，必须如实告诉用户。
            svc.state = "degraded"
            self.progress("  AI 引擎  : 未安装本地推理库，AI 问答降级为规则引擎")

    # ------------------------------------------------------------------ 后端
    def _backend_command(self) -> list[str]:
        frozen = self.layout.root / self.layout.exe_name("AIMS-Backend")
        if frozen.is_file():
            return [str(frozen)]
        script = self.layout.root / "run_standalone.py"
        if script.is_file():
            return [sys.executable, str(script)]
        # 源码运行：本文件所在的仓库根
        repo = Path(__file__).resolve().parent.parent.parent
        return [sys.executable, str(repo / "run_standalone.py")]

    def _spawn_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env.items() if not k.startswith("#")})
        env["AIMS_ROOT"] = str(self.root)
        env["AIMS_LAUNCHED_BY"] = "launcher"
        env["AIMS_LAUNCHER_TOKEN"] = self.launcher_token
        # 让后端在启动器意外消失时自行退出，避免留下占端口的孤儿进程
        env["AIMS_PARENT_PID"] = str(os.getpid())
        # 打开界面由启动器负责（带托盘、只开一次），后端自己不要再开一个浏览器
        env["AIMS_OPEN_BROWSER"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        if IS_WINDOWS:
            env["PYTHONIOENCODING"] = "utf-8"
        return env

    def start_backend(self) -> None:
        svc = self.state["backend"]
        existing = _health(self.api_port)
        if existing and existing.get("app"):
            svc.state, svc.detail = "running", f"已在运行（PID 非本进程子进程）"
            self.started_backend_here = False
            self.progress(f"  后端服务 : 已在运行（{existing.get('version')}）")
            return
        if not port_free(self.api_port):
            svc.state, svc.detail = "failed", f"端口 {self.api_port} 被占用"
            raise SupervisorError(
                f"端口 {self.api_port} 已被其它程序占用，AIMS 无法启动。\n\n"
                "请关闭占用该端口的程序，或修改 config\\aims.env 中的 PORT 后重试。"
            )
        cmd = self._backend_command()
        log_path = self.layout.logs_dir / "backend.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        svc.state = "starting"
        self.progress(f"  后端服务 : 启动 {Path(cmd[0]).name} ……")
        log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launcher starting backend =====\n")
        log_file.flush()
        kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "env": self._spawn_env(),
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.backend = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            svc.state, svc.detail = "failed", str(exc)
            raise SupervisorError(f"无法启动后端程序：{cmd[0]}\n{exc}") from exc
        finally:
            log_file.close()
        self.started_backend_here = True

        deadline = time.time() + 180
        delay = 0.4
        while time.time() < deadline:
            if self.backend.poll() is not None:
                code = self.backend.returncode
                detail = _tail(log_path, 40)
                svc.state, svc.detail = "failed", f"后端退出（代码 {code}）"
                raise SupervisorError(
                    f"后端服务启动后立即退出（退出代码 {code}）。\n\n{detail}\n\n"
                    f"完整日志：{log_path}"
                )
            info = _health(self.api_port)
            if info and info.get("status") == "ok":
                svc.state = "running"
                svc.detail = f"v{info.get('version')} pid={self.backend.pid}"
                self.progress(f"  后端服务 : 就绪（v{info.get('version')}，PID {self.backend.pid}）")
                return
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)
        svc.state, svc.detail = "failed", "就绪超时"
        raise SupervisorError(
            f"后端服务在 180 秒内没有就绪。\n\n{_tail(log_path, 40)}\n\n完整日志：{log_path}"
        )

    # ------------------------------------------------------------------ 界面
    def ui_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}/ui"

    # ------------------------------------------------------------- 启动/停止
    def start_all(self) -> dict[str, Any]:
        self.load()
        if managed_mode(self.layout):
            self.start_database()
        else:
            self.state["database"].state = "skipped"
            self.progress("  数据库   : 非托管安装（SQLite），跳过")
        self.start_ai()
        self.start_backend()
        self.state["ui"].state = "ready"
        return self.status()

    def stop_all(self, *, stop_db: bool | None = None) -> dict[str, Any]:
        with self._stop_lock:
            if self._stopping:
                return self.status()
            self._stopping = True
        try:
            if self.backend is not None and self.backend.poll() is None:
                self.progress("  正在停止后端服务 ……")
                self._terminate_tree(self.backend)
                self.state["backend"].state = "stopped"
            elif _health(self.api_port) and not self.started_backend_here:
                # 别人的进程：只提示，不动手（可能是管理员起的服务）
                logger.info("backend at :%s was not started by this launcher; leaving it running", self.api_port)
            self.backend = None

            want_db_stop = self.started_db_here if stop_db is None else stop_db
            st_mode = (state_mod.read_state(self.layout).get("pg") or {}).get("mode")
            if want_db_stop and st_mode != "service" and self.pg is not None:
                self.progress("  正在停止内置数据库 ……")
                try:
                    if self.pg.status() == "running":
                        self.pg.stop()
                except PgError as exc:
                    logger.warning("stop database: %s", exc)
                self.state["database"].state = "stopped"
            return self.status()
        finally:
            self._stopping = False

    def _terminate_tree(self, proc: subprocess.Popen, *, grace: float = 12.0) -> None:
        """先礼后兵：CTRL_BREAK 让 uvicorn 走正常关停（关连接池、落盘），再强杀进程树。"""
        if proc.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        logger.warning("backend did not stop in %.0fs, killing tree", grace)
        if IS_WINDOWS:
            try:
                # CREATE_NO_WINDOW：taskkill 是控制台程序，在 GUI 启动器里
                # 直接跑会闪出黑色 cmd 窗口，必须显式压掉。
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:  # noqa: BLE001
                proc.kill()
        else:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("backend pid %s still alive after kill", proc.pid)

    def shutdown(self) -> None:
        try:
            self.stop_all()
        finally:
            from app.deploy import runtime

            runtime.shutdown(self.layout, started_here=self.started_db_here)

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "root": str(self.root),
            "api_port": self.api_port,
            "ui_url": self.ui_url(),
            "services": {k: v.as_dict() for k, v in self.state.items()},
            "backend_pid": self.backend.pid if self.backend and self.backend.poll() is None else None,
        }
        if self.pg is not None:
            info["postgres"] = {
                "port": self.pg.port,
                "initialized": self.pg.initialized,
                "ready": self.pg.is_ready(timeout=1.0) if self.pg.initialized else False,
            }
        health = _health(self.api_port, timeout=1.5)
        info["health"] = health
        return info


# --------------------------------------------------------------------------- #
def _default_root() -> Path:
    override = os.environ.get("AIMS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])
