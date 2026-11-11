"""安装布局解析（程序目录 / 数据目录分离）。

两种部署形态，同一份代码：

1. **产品安装（Windows 离线安装包）** —— 程序与数据分离，这是默认形态：

       C:\\Program Files\\AIMS\\           程序（只读，升级时整体替换）
           AIMS.exe                       桌面启动器（托盘 + 服务编排）
           AIMS-Backend\\AIMS-Backend.exe 后端服务
           frontend.html                  单文件界面（无外网依赖）
           cores\\                          规则包 / 本地 JS 库
           pgsql\\bin,lib,share            内置 PostgreSQL 与 pgvector
           tools\\*.ps1                    运维脚本
       C:\\ProgramData\\AIMS\\             数据（升级/卸载都不动，除非用户勾选清除）
           Config\\aims.env                生成的配置（含口令，仅管理员可读）
           Database\\                       PostgreSQL 集群（业务数据）
           Models\\                         GGUF 模型（内置 + 用户下载）
           Logs\\                           启动器/后端/数据库日志
           Uploads\\                        上传与导出文件
           Backup\\                         自动与手动备份

   安装程序通过环境变量 `AIMS_DATA_ROOT` 指定数据根目录，程序目录永远从
   `AIMS_ROOT`（或 EXE 所在目录）取，二者解耦 —— 这样"AUTO_UPGRADE 覆盖安装"
   只替换 Program Files 下的文件，业务数据一个字节都不会被碰到。

2. **源码/绿色运行** —— 不设 `AIMS_DATA_ROOT` 时退化为就地存放：

       <repo>\\pgsql\\data, <repo>\\config\\aims.env, <repo>\\aims_data\\{logs,backups}

   这是开发和自动化测试的形态，也是老版本 SQLite EXE 的形态。

每个路径都可以用环境变量单独覆盖，因此这套代码在 Linux 上也能被完整测试
（把 AIMS_PG_BINDIR 指向 /usr/lib/postgresql/16/bin 即可）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def install_root() -> Path:
    """程序目录：拥有 AIMS.exe / pgsql / frontend.html 的那个目录。"""
    override = os.environ.get("AIMS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Layout:
    root: Path
    #: 数据根目录（产品安装为 C:\ProgramData\AIMS；绿色运行为 None）
    data_root: Path | None = None

    # ------------------------------------------------------------------ 形态
    @property
    def product_mode(self) -> bool:
        """程序目录与数据目录是否分离（= 是否由安装程序部署）。"""
        return bool(os.environ.get("AIMS_DATA_ROOT")) or self.data_root is not None

    @property
    def base(self) -> Path:
        """所有"数据侧"路径的根。"""
        if self.data_root is not None:
            return self.data_root
        raw = os.environ.get("AIMS_DATA_ROOT")
        return Path(raw).expanduser().resolve() if raw else self.root

    # ---------------------------------------------------------------- 程序侧
    @property
    def pgsql(self) -> Path:
        return _env_path("AIMS_PG_HOME", self.root / "pgsql")

    @property
    def pg_bin(self) -> Path:
        return _env_path("AIMS_PG_BINDIR", self.pgsql / "bin")

    @property
    def pg_share(self) -> Path:
        return _env_path("AIMS_PG_SHARE", self.pgsql / "share")

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"

    # ---------------------------------------------------------------- 数据侧
    @property
    def config_dir(self) -> Path:
        return self.base / "Config" if self.product_mode else self.root / "config"

    @property
    def env_file(self) -> Path:
        return self.config_dir / "aims.env"

    @property
    def pg_data(self) -> Path:
        default = (self.base / "Database") if self.product_mode else (self.pgsql / "data")
        return _env_path("AIMS_PG_DATA", default)

    @property
    def data_dir(self) -> Path:
        """日志/备份/状态文件所在目录（绿色运行为 aims_data）。"""
        default = self.base if self.product_mode else (self.root / "aims_data")
        return _env_path("AIMS_DATA_DIR", default)

    @property
    def logs_dir(self) -> Path:
        return (self.base / "Logs") if self.product_mode else (self.data_dir / "logs")

    @property
    def backups_dir(self) -> Path:
        default = (self.base / "Backup") if self.product_mode else (self.data_dir / "backups")
        return _env_path("AIMS_BACKUP_DIR", default)

    @property
    def uploads_dir(self) -> Path:
        default = (self.base / "Uploads") if self.product_mode else (self.data_dir / "uploads")
        return _env_path("AIMS_UPLOAD_DIR", default)

    @property
    def models_dir(self) -> Path:
        """用户模型的落盘位置；内置模型只读地放在 <程序目录>\\cores\\models。"""
        default = (self.base / "Models") if self.product_mode else (self.root / "cores" / "models")
        return _env_path("AIMS_MODELS_DIR", default)

    @property
    def bundled_models_dir(self) -> Path:
        return self.root / "cores" / "models"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "deployment.json"

    @property
    def sqlite_db(self) -> Path:
        return self.data_dir / "aims.db"

    # -------------------------------------------------------------------- 工具
    def exe_name(self, stem: str) -> str:
        return f"{stem}.exe" if IS_WINDOWS else stem

    def ensure_dirs(self) -> None:
        # 程序目录也要建：安装包里由安装程序创建，但源码/便携部署时它可能还不存在，
        # 而 PostgreSQL 子进程是以它作为工作目录启动的（cwd 缺失会被误报成"找不到程序"）。
        for d in (self.root, self.config_dir, self.data_dir, self.backups_dir, self.logs_dir,
                  self.uploads_dir, self.models_dir, self.pg_data.parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                # 只读介质上的目录（极少见）不该阻断启动；具体哪一项不可写由 doctor 报告。
                pass

    def describe(self) -> dict[str, str]:
        return {
            "程序目录": str(self.root),
            "数据目录": str(self.base),
            "配置": str(self.env_file),
            "数据库": str(self.pg_data),
            "日志": str(self.logs_dir),
            "备份": str(self.backups_dir),
            "模型": str(self.models_dir),
        }


def load_layout(root: Path | None = None) -> Layout:
    return Layout(root or install_root())


def managed_mode(layout: Layout | None = None) -> bool:
    """True when this install owns a bundled PostgreSQL cluster.

    老版本 SQLite EXE（或从裸仓库 `python run_standalone.py`）没有 pgsql/bin，
    因此它们的行为完全不变。
    """
    layout = layout or load_layout()
    if os.environ.get("AIMS_MANAGED_RUNTIME", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("AIMS_DB_MODE", "").strip().lower() in ("sqlite", "external"):
        return False
    return (layout.pg_bin / layout.exe_name("pg_ctl")).is_file() and layout.pg_data.is_dir()
