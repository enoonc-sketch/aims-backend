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
        "ADMIN_PASSWORD": new_bootstrap_password,
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


def build_env(layout: Layout, state: dict, secrets: dict, *, port: int, api_port: int,
              admin_username: str = "admin") -> dict[str, str]:
    """The full, explicit configuration written at install time.

    Everything the app needs is spelled out here instead of relying on defaults, so
    `config/aims.env` doubles as the deployment documentation for a customer site.
    """
    from app.deploy import APP_VERSION

    pg = state.get("pg", {})
    host = pg.get("host", "127.0.0.1")
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

        "BOOTSTRAP_ADMIN_USERNAME": admin_username,
        "BOOTSTRAP_ADMIN_PASSWORD": secrets["ADMIN_PASSWORD"],
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
        "AIMS_BACKUP_KEEP": "14",
        "AIMS_BACKUP_DIR": str(layout.backups_dir),
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
