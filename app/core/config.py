"""Application configuration, loaded from environment variables / .env

Security posture (enforced at import time):
  * PRODUCTION: SECRET_KEY must be injected from outside (env var, secret manager,
    or an untracked .env). Missing / short / well-known placeholder values abort
    startup.
  * DEVELOPMENT / standalone (APP_ENV != production): a missing SECRET_KEY is
    replaced by a random key that is generated once and persisted next to the
    database (aims_data/.dev_secret_key), so tokens survive restarts without
    any configuration. A weak/short key only logs a warning.
  * A development administrator (admin / admin123) may be created on an empty
    database (ALLOW_DEV_ADMIN). It is refused in production.
  * CORS_ORIGINS is an explicit whitelist. "*" and "null" are always rejected.
  * In production, HTTPS is mandatory (direct TLS or a trusted TLS-terminating
    reverse proxy) and CORS origins must be https:// URLs.
"""
import logging
import secrets as _secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROD_ENVS = {"prod", "production"}
_log = logging.getLogger(__name__)

# Placeholder / example values that must never be used as a real signing key.
_WEAK_SECRETS = {
    "change-me",
    "changeme",
    "change-me-to-a-random-secret",
    "secret",
    "secret-key",
    "your-secret-key",
    "please-change-me",
    "default",
    "dev",
    "test",
}
_MIN_SECRET_LEN = 32


def _dev_secret_path(cfg: "Settings") -> Path:
    if cfg.DATABASE_URL.startswith("sqlite"):
        raw = cfg.DATABASE_URL.replace("sqlite:///", "", 1)
        if raw and raw != ":memory:":
            return Path(raw).resolve().parent / ".dev_secret_key"
    return Path.cwd() / "aims_data" / ".dev_secret_key"


def _load_or_create_dev_secret(cfg: "Settings") -> str:
    """Development only: reuse a persisted random key, or create one. Falls back to
    an in-memory random key (tokens then reset on restart) if the disk is read-only."""
    path = _dev_secret_path(cfg)
    try:
        if path.is_file():
            val = path.read_text(encoding="utf-8").strip()
            if len(val) >= _MIN_SECRET_LEN:
                return val
        path.parent.mkdir(parents=True, exist_ok=True)
        val = _secrets.token_urlsafe(64)
        path.write_text(val, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _log.warning("SECRET_KEY not set: generated a development key at %s "
                     "(set SECRET_KEY explicitly for production).", path)
        return val
    except OSError:
        _log.warning("SECRET_KEY not set and no writable data dir: using a temporary key "
                     "(sessions reset on restart).")
        return _secrets.token_urlsafe(64)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "AIMS Backend"
    APP_ENV: str = "development"
    API_V1_PREFIX: str = "/api/v1"

    # ---- Secrets ----
    # Production: must be injected externally (>= 32 chars, not a placeholder).
    # Development: if empty, a random key is generated once and persisted.
    SECRET_KEY: str = Field(default="", description="Injected externally in production; >= 32 chars")

    # ---- Tokens ----
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "aims-backend"
    JWT_AUDIENCE: str = "aims-api"

    # ---- Login throttling ----
    LOGIN_MAX_FAILURES: int = 5              # per (client IP + username) pair, inside the window
    LOGIN_MAX_FAILURES_PER_IP: int = 20      # per client IP over all usernames (spraying ceiling)
    LOGIN_LOCKOUT_MINUTES: int = 15

    # ---- Bootstrap admin (only used when the users table is empty) ----
    BOOTSTRAP_ADMIN_USERNAME: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""
    # Default administrator admin / admin123, created only on an EMPTY users table when no
    # BOOTSTRAP_ADMIN_* is given, so a fresh desktop/dev instance can always be logged into.
    # None => automatic: ON outside production (1.0.0 had turned this OFF, which left the
    # standalone build with no account at all; the owner asked for admin/admin123 as default).
    # Set ALLOW_DEV_ADMIN=false to opt out. ALWAYS refused in production, whatever this says:
    # production must inject BOOTSTRAP_ADMIN_* or run `python -m app.cli create-admin`.
    ALLOW_DEV_ADMIN: bool | None = None
    # 默认账号（admin/admin123）首次登录必须先改密码。只影响“用默认口令新建”的开发管理员；
    # 自定义口令（BOOTSTRAP_ADMIN_*）建的账号、已有账号都不受影响。
    DEV_ADMIN_FORCE_PASSWORD_CHANGE: bool = True
    DEV_ADMIN_USERNAME: str = "admin"
    DEV_ADMIN_PASSWORD: str = "admin123"
    # Demo devices/alarms are never seeded in production; opt-in elsewhere.
    SEED_DEMO_DATA: bool = False

    # Default to local SQLite for standalone / EXE mode.
    DATABASE_URL: str = "sqlite:///./aims_data/aims.db"

    # PostgreSQL connection pool tuning (report item D-5; only used for non-SQLite URLs)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 1800  # seconds; stay well below typical server idle timeouts

    AUTO_INIT_DB: bool = True

    AI_PROVIDER: str = "openai"
    AI_API_KEY: str = ""
    AI_API_BASE: str = "https://api.openai.com/v1"
    AI_CHAT_MODEL: str = "gpt-4o-mini"
    AI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    AI_EMBEDDING_DIM: int = 1536
    # How knowledge_chunks.embedding is stored on PostgreSQL:
    #   auto   -> pgvector `vector(N)` when the package is importable (Docker/production default)
    #   vector -> force pgvector (fails loudly if it is missing)
    #   json   -> force JSON, for a PostgreSQL build without the pgvector extension
    #             (the Windows offline installer sets this when vector.so could not be
    #             built; retrieval then uses the application-side cosine path)
    EMBEDDING_STORAGE: str = "auto"

    # --- Local / offline model runtime (real GGUF models via llama.cpp) ---
    MODELS_DIR: str = "cores/models"
    AI_LOCAL_LLM_ENABLED: bool = True
    AI_LOCAL_LLM_THREADS: int = 4
    AI_LOCAL_LLM_CTX: int = 4096
    AI_LOCAL_LLM_MAX_TOKENS: int = 512
    AI_LOCAL_LLM_GPU_LAYERS: int = 0
    AI_LOCAL_EMBEDDING_ENABLED: bool = True

    # --- External vendor-platform data source (web-scraping module) ---
    EXTERNAL_DOWNLOAD_DIR: str = "data/external_downloads"
    PLAYWRIGHT_HEADLESS: bool = True

    # ---- CORS: explicit whitelist only (comma separated origins) ----
    # Empty = no cross-origin access. The bundled UI is served same-origin at /ui
    # and does not need CORS at all.
    CORS_ORIGINS: str = ""

    # ---- HTTPS / transport ----
    # None => automatic: True in production, False otherwise.
    REQUIRE_HTTPS: bool | None = None
    SSL_CERTFILE: str = ""                   # direct TLS termination by uvicorn
    SSL_KEYFILE: str = ""
    # Comma separated proxy IPs/CIDRs whose X-Forwarded-Proto/For are trusted
    # (TLS terminated upstream). "*" only if the API port is unreachable except
    # via the proxy (e.g. private docker network).
    TRUSTED_PROXY_IPS: str = ""
    ALLOWED_HOSTS: str = ""                  # optional Host header whitelist
    ENABLE_DOCS: bool | None = None          # None => enabled outside production

    # ---- Upload safety ----
    MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024
    MAX_UPLOAD_ROWS: int = 500_000
    MAX_UPLOAD_COLUMNS: int = 500
    MAX_XLSX_UNCOMPRESSED_BYTES: int = 200 * 1024 * 1024
    MAX_XLSX_ENTRIES: int = 2000
    MAX_XLSX_COMPRESSION_RATIO: int = 200

    # ---- Audit ----
    AUDIT_ENABLED: bool = True

    # --- 请求限流（M-7，总方案 3.4）。单机桌面部署用内存令牌桶即可 ---
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_API_PER_WINDOW: int = 600      # 普通 API：每 IP 每分钟
    RATE_LIMIT_AUTH_PER_WINDOW: int = 20      # 登录/刷新：每 IP 每分钟

    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in _PROD_ENVS

    @property
    def dev_admin_allowed(self) -> bool:
        if self.is_production:
            return False
        if self.ALLOW_DEV_ADMIN is None:
            return True  # unset: ON outside production (see ALLOW_DEV_ADMIN above)
        return bool(self.ALLOW_DEV_ADMIN)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [p.strip() for p in self.TRUSTED_PROXY_IPS.split(",") if p.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]

    @property
    def https_required(self) -> bool:
        return self.is_production if self.REQUIRE_HTTPS is None else bool(self.REQUIRE_HTTPS)

    @property
    def tls_direct(self) -> bool:
        return bool(self.SSL_CERTFILE and self.SSL_KEYFILE)

    @property
    def docs_enabled(self) -> bool:
        # 审计项 L-7：离线交付版（PyInstaller frozen）默认也不开 /docs——
        # 给现场暴露完整接口地图没有价值，排障用 ENABLE_DOCS=true 显式打开。
        if self.ENABLE_DOCS is not None:
            return bool(self.ENABLE_DOCS)
        if self.is_production:
            return False
        import sys as _sys
        return not getattr(_sys, "frozen", False)

    @model_validator(mode="after")
    def validate_security(self):
        # --- SECRET_KEY ---
        key = (self.SECRET_KEY or "").strip()
        weak = key.lower() in _WEAK_SECRETS or len(key) < _MIN_SECRET_LEN
        if self.is_production:
            # Production: externally injected, strong, never a placeholder.
            if not key:
                raise ValueError(
                    "SECRET_KEY is not set. It must be injected externally (environment variable, "
                    "secret manager or untracked .env). Generate one with: "
                    "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
                )
            if weak:
                raise ValueError(
                    f"SECRET_KEY must be a random value of at least {_MIN_SECRET_LEN} characters "
                    "and must not be a well-known placeholder"
                )
        else:
            if not key:
                key = _load_or_create_dev_secret(self)
            elif weak:
                _log.warning("SECRET_KEY is weak/short (development mode only: accepted). "
                             "Use a random value of >= %d chars before deploying.", _MIN_SECRET_LEN)
        self.SECRET_KEY = key

        # --- CORS whitelist: "*" and "null" are never acceptable ---
        for origin in self.cors_origin_list:
            if origin in {"*", "null"}:
                raise ValueError(f"CORS_ORIGINS must not contain {origin!r}; list explicit origins instead")
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
                raise ValueError(f"Invalid CORS origin {origin!r}: expected scheme://host[:port]")
            if self.is_production and parsed.scheme != "https":
                raise ValueError(f"Production CORS origin must use https: {origin!r}")

        # --- Token lifetimes / limits sanity ---
        if not 1 <= self.ACCESS_TOKEN_EXPIRE_MINUTES <= 60:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be between 1 and 60 (use refresh tokens for longevity)")
        if not 1 <= self.REFRESH_TOKEN_EXPIRE_DAYS <= 90:
            raise ValueError("REFRESH_TOKEN_EXPIRE_DAYS must be between 1 and 90")
        if self.LOGIN_MAX_FAILURES < 1 or self.LOGIN_MAX_FAILURES_PER_IP < 1 or self.LOGIN_LOCKOUT_MINUTES < 1:
            raise ValueError("Login throttling limits must be positive")
        if str(self.EMBEDDING_STORAGE).strip().lower() not in {"auto", "vector", "json"}:
            raise ValueError("EMBEDDING_STORAGE must be one of: auto, vector, json")

        # --- Production transport requirements ---
        if self.is_production:
            if self.REQUIRE_HTTPS is False:
                raise ValueError("REQUIRE_HTTPS cannot be disabled in production")
            if not (self.tls_direct or self.trusted_proxy_list):
                raise ValueError(
                    "Production requires HTTPS: set SSL_CERTFILE+SSL_KEYFILE (direct TLS) "
                    "or TRUSTED_PROXY_IPS (TLS terminated by a trusted reverse proxy)"
                )
            if self.SEED_DEMO_DATA:
                raise ValueError("SEED_DEMO_DATA must not be enabled in production")
            if self.ALLOW_DEV_ADMIN:
                raise ValueError("ALLOW_DEV_ADMIN must not be enabled in production")
        if bool(self.SSL_CERTFILE) != bool(self.SSL_KEYFILE):
            raise ValueError("SSL_CERTFILE and SSL_KEYFILE must be set together")
        return self

    @property
    def models_dir_path(self) -> Path:
        p = Path(self.MODELS_DIR)
        if not p.is_absolute():
            p = Path.cwd() / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def external_download_dir_path(self) -> Path:
        p = Path(self.EXTERNAL_DOWNLOAD_DIR)
        if not p.is_absolute():
            p = Path.cwd() / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        raw = self.DATABASE_URL.replace("sqlite:///", "", 1)
        return Path(raw)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Clear cache and rebuild Settings from current environment (needed for EXE)."""
    get_settings.cache_clear()
    return get_settings()


# Module-level convenience; prefer get_settings() / reload_settings() in entrypoints
settings = get_settings()
