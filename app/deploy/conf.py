"""Secrets that must survive upgrades: JWT signing key, PostgreSQL passwords, the
first administrator's password.

They live in ``<data root>/Config/secrets.json`` (created once, mode 0600) and *not*
in the generated ``Config/aims.env``, because:

  * an upgrade/reinstall rewrites config/aims.env from scratch — regenerating the
    JWT key there would silently invalidate every session and make the passwords
    stored for external vendor sites (services/crypto_util.py) undecryptable;
  * the Config directory is what the uninstaller keeps, so "uninstall, reinstall,
    restore the backup" keeps working end to end.

Regenerating them requires deleting that file deliberately (see `rotate`).
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
import string
from pathlib import Path
from typing import Any

from app.deploy.paths import Layout, load_layout

_ALNUM = string.ascii_letters + string.digits


def new_password(length: int = 32) -> str:
    """Password made of [A-Za-z0-9] only.

    Pure alphanumerics (32 chars ≈ 190 bits) are chosen on purpose: this value ends
    up in a URL (DATABASE_URL is parsed by SQLAlchemy's URL splitter), in a
    pgpass-style environment variable and in PowerShell/Inno strings. Special
    characters there are a reliable source of "%" / "&" / quote bugs that only
    show up on a customer's machine.
    """
    return "".join(_secrets.choice(_ALNUM) for _ in range(length))


def new_secret_key() -> str:
    return _secrets.token_urlsafe(64)


# 安装版（桌面/离线）的默认管理员口令，与独立桌面版空库启动一致（admin / admin123）。
# 它不是随机值：用户装完直接用 admin/admin123 登录，登录后在「系统设置 → 修改密码」里改。
# 生产环境（APP_ENV=production）仍然拒绝这个默认账号，见 app/core/config.py。
DEFAULT_ADMIN_PASSWORD = "admin123"
# 1.0.7 及更早的全新安装默认口令是 admin；老库里还在用它的默认管理员同样要按“默认口令”对待。
LEGACY_DEFAULT_ADMIN_PASSWORDS = ("admin",)


def new_bootstrap_password(length: int = 16) -> str:
    """First-login password: alphanumeric, avoids look-alike pairs, easy to read out."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ" + "abcdefghijkmnopqrstuvwxyz" + "23456789"
    return "".join(_secrets.choice(alphabet) for _ in range(length))


def secrets_path(layout: Layout | None = None) -> Path:
    """口令文件放在 Config 目录，与 aims.env 同处（产品安装下 = ProgramData\\AIMS\\Config）。

    0.5.x 的旧安装把 secrets.json 放在 aims_data/ 下。覆盖升级不能因为"文件换了位置"
    就把 JWT 密钥和数据库口令判为丢失（那会让所有已登录会话失效、外部站点口令无法
    解密），所以这里对旧位置做只读兼容：新位置不存在、旧位置存在时继续用旧位置，
    下一次写入口令时自然会落到新位置。
    """
    layout = layout or load_layout()
    primary = layout.config_dir / "secrets.json"
    legacy = layout.data_dir / "secrets.json"
    if not primary.is_file() and legacy.is_file():
        return legacy
    return primary


def ensure_secrets(layout: Layout | None = None, *, rotate: bool = False) -> dict[str, Any]:
    """Read secrets.json, creating it (and any missing field) when needed."""
    layout = layout or load_layout()
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_path(layout)
    data: dict[str, Any] = {}
    if path.is_file() and not rotate:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    changed = False
    defaults = {
        "SECRET_KEY": new_secret_key,
        "DB_PASSWORD": new_password,
        "DB_SUPERUSER_PASSWORD": new_password,
        "ADMIN_PASSWORD": lambda: DEFAULT_ADMIN_PASSWORD,
    }
    for key, factory in defaults.items():
        if rotate or not data.get(key):
            data[key] = factory()
            changed = True
    if changed or not path.is_file():
        _write_private(path, data)
    return data


def _write_private(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def read_secrets(layout: Layout | None = None) -> dict[str, Any]:
    path = secrets_path(layout)
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def first_login_credentials_path(layout: Layout | None = None) -> Path:
    layout = layout or load_layout()
    return layout.config_dir / "first-login.txt"


def write_first_login_credentials(layout: Layout, *, username: str, password: str,
                                  api_port: int = 8000) -> Path:
    """首次安装时把管理员初始账号写成人能读的文件（开始菜单/安装完成页都链到它）。

    全新安装的默认账号是 admin / admin123；实施人员用 --admin-password 自定义过口令时，
    这里写的是自定义值。文件放在与 secrets.json 相同的 Config 目录（ACL 同样受限）。
    """
    path = first_login_credentials_path(layout)
    if password in LEGACY_DEFAULT_ADMIN_PASSWORDS:   # 旧版 secrets 里存的 admin：应用现在实际建的是 admin123
        password = DEFAULT_ADMIN_PASSWORD
    is_default = password == DEFAULT_ADMIN_PASSWORD
    text = (
        "AIMS 首次登录凭据（安装时自动生成）\n"
        "====================================\n"
        "\n"
        "登录入口：桌面快捷方式「AIMS」，或浏览器打开\n"
        f"    http://127.0.0.1:{api_port}/frontend.html\n"
        "\n"
        f"用户名：{username}\n"
        f"初始密码：{password}\n"
        "\n"
        "重要提示：\n"
        + ("  1. 这是默认账号：首次登录时系统会要求先在「系统设置 → 修改密码」里改成自己的密码，改完才能使用其他功能。\n"
           if is_default else
           "  1. 首次登录后请立即在「系统设置 → 修改密码」中改成自己的密码。\n")
        + "  2. 密码修改后本文件内容即失效，建议尽快删除本文件。\n"
        "  3. 部署人员也可自定义初始密码：\n"
        "         AIMS-Backend.exe deploy provision --admin-password <新密码>\n"
    )
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# --------------------------------------------------------------------------- #
# Generated configuration file (config/aims.env)
# --------------------------------------------------------------------------- #
def database_url(*, host: str, port: int, user: str, password: str, database: str) -> str:
    """SQLAlchemy URL for the bundled cluster.

    The password is URL-quoted defensively even though new_password() only emits
    alphanumerics — a value restored from an older install or set by an
    administrator may contain anything.
    """
    from urllib.parse import quote

    return (f"postgresql+psycopg2://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{database}")


def admin_account_env(admin_username: str, admin_password: str | None) -> dict[str, str]:
    """首个管理员账号写进 aims.env 的方式。

    * 口令是默认的 admin123（全新安装；旧版遗留的 admin 同样按默认处理）：**不写** BOOTSTRAP_ADMIN_*，也**不写**
      ALLOW_DEV_ADMIN —— 让应用按“非生产环境、空库”自动创建开发管理员 admin/admin123
      （与桌面独立版一致）。这样以后把 APP_ENV 改成 production 时，配置里没有任何
      强制开启默认账号的项，应用照常启动且不存在默认账号。用户名不是 admin 时用
      DEV_ADMIN_USERNAME 带上。
    * 口令是实施人员自定义的（--admin-password）或旧安装遗留的随机口令：照旧走
      BOOTSTRAP_ADMIN_*（必须满足口令强度策略）。
    """
    username = (admin_username or "admin").strip() or "admin"
    password = admin_password or DEFAULT_ADMIN_PASSWORD
    if password == DEFAULT_ADMIN_PASSWORD or password in LEGACY_DEFAULT_ADMIN_PASSWORDS:
        return {"DEV_ADMIN_USERNAME": username} if username != "admin" else {}
    return {"BOOTSTRAP_ADMIN_USERNAME": username, "BOOTSTRAP_ADMIN_PASSWORD": password}


def build_env(layout: Layout, state: dict, secrets: dict, *, port: int, api_port: int,
              admin_username: str = "admin") -> dict[str, str]:
    """The full, explicit configuration written at install time.

    Everything the app needs is spelled out here instead of relying on defaults, so
    `config/aims.env` doubles as the deployment documentation for a customer site.
    """
    from app.deploy import APP_VERSION

    pg = state.get("pg", {})
    host = pg.get("host", "127.0.0.1")
    admin_env = admin_account_env(admin_username, secrets.get("ADMIN_PASSWORD"))
    values: dict[str, str] = {
        "# AIMS Backend — generated by the installer; re-run `deploy provision` after editing": "",
        "APP_NAME": "AIMS Backend",
        # `production` would force HTTPS + reject the loopback UI; a single-user
        # desktop install leaves TLS to the optional reverse proxy.
        "APP_ENV": "development",
        "AIMS_PRODUCT_VERSION": APP_VERSION,
        "AIMS_DEPLOYMENT": "windows-offline",

        "DATABASE_URL": database_url(host=host, port=int(port), user=pg.get("app_user", "aims"),
                                     password=secrets["DB_PASSWORD"],
                                     database=pg.get("database", "aims")),
        "AUTO_INIT_DB": "true",
        # Idempotent "add missing columns/tables" step on every start; a no-op when
        # the schema is already current. This is what makes覆盖升级 data-safe.
        "AUTO_UPGRADE_SCHEMA": "true",

        "SECRET_KEY": secrets["SECRET_KEY"],
        "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
        "REFRESH_TOKEN_EXPIRE_DAYS": "7",

        **admin_env,
        "SEED_DEMO_DATA": "true",

        "HOST": "127.0.0.1",
        "PORT": str(api_port),
        "ENABLE_DOCS": "true",
        "CORS_ORIGINS": "",

        # Offline AI engine: bundled GGUF models under cores/models, llama.cpp
        # inside the EXE. Leave AI_API_KEY empty => no traffic ever leaves the box.
        "AI_PROVIDER": "openai",
        "AI_API_KEY": "",
        "MODELS_DIR": str(layout.models_dir),
        "AI_LOCAL_LLM_ENABLED": "true",
        "AI_LOCAL_EMBEDDING_ENABLED": "true",
        "AI_LOCAL_LLM_THREADS": "0",  # 0 => auto (physical cores, capped at 8)

        "EXTERNAL_DOWNLOAD_DIR": str(layout.root / "data" / "external_downloads"),
        "MAX_UPLOAD_BYTES": str(64 * 1024 * 1024),

        "AIMS_MANAGED_RUNTIME": "1",
        "AIMS_PG_PORT": str(port),
        # 集群与数据根必须落盘。provision 时可能用了非默认位置（D 盘、独立数据盘、
        # 迁移后的目录）——只存在当时的环境变量里的话，重启后（安装器/启动器/计划任务/
        # 直接用 EXE 启动）拿不到这些值，就会去默认路径找 PG_VERSION，然后报
        # "数据库程序已就位，但数据目录不存在"：现象是"装完打不开"，原因却看不出。
        "AIMS_PG_DATA": str(layout.pg_data),
        "AIMS_PG_BINDIR": str(layout.pg_bin),
        "AIMS_DATA_ROOT": str(layout.base),
        "AIMS_BACKUP_KEEP": "14",
        "AIMS_BACKUP_DIR": str(layout.backups_dir),

        # 日志（1.0.0）：text 是给人看的默认格式；改成 json 即为单行 JSON，
        # 便于 logstash/loki 等采集器直接消费。日志级别同理。
        "AIMS_LOG_FORMAT": "text",
        "AIMS_LOG_LEVEL": "INFO",
    }
    return values


def render_env(values: dict[str, str]) -> str:
    lines = [
        "# =====================================================================",
        "#  AIMS Backend — local (offline) deployment configuration",
        "#  Generated by the installer. Edited by hand? Re-run:",
        "#      AIMS-Backend.exe deploy provision --keep-config",
        "#  or use tools\\Edit-AimsConfig.ps1 (asks for elevation).",
        "# =====================================================================",
        "",
    ]
    for key, value in values.items():
        if key.startswith("#"):
            lines.append(f"# {key[1:].strip()}" if key.strip("# ") else "")
            continue
        if value is None:
            continue
        lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip() + "\n"


def write_env(layout: Layout, values: dict[str, str]) -> Path:
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    path = layout.env_file
    path.write_text(render_env(values), encoding="utf-8")
    _harden_acl(path)
    return path


def _harden_acl(path: Path) -> None:
    """Best effort: keep the file readable only by the local Users group.

    On Windows the installer already restricts config\\ to Administrators + Users;
    on POSIX we drop group/other bits. Failures are ignored (read-only media).
    """
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def apply_env(values: dict[str, str], *, override: bool = False) -> None:
    """Push a config dict into os.environ before the Settings object is built.

    Real environment variables win by default (so `set DATABASE_URL=...` still
    overrides the file for support/debug), which mirrors how the rest of the app
    treats .env.
    """
    for key, value in values.items():
        if key.startswith("#") or value is None or value == "":
            continue
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def load_env(layout: Layout | None = None) -> dict[str, str]:
    layout = layout or load_layout()
    return parse_env_file(layout.env_file)


def env_int(values: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(values.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
