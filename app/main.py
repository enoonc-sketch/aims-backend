"""AIMS Backend — FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.deploy import APP_VERSION
from app.core.metrics import MetricsMiddleware
from app.core.middleware import (
    RateLimitMiddleware,
    AuditMiddleware,
    HTTPSEnforcementMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    make_db_audit_writer,
)

from app.core.logging_setup import configure_logging

# 文本格式是默认值（现场人看日志）；AIMS_LOG_FORMAT=json 时输出单行 JSON，供采集器消费。
configure_logging()
logger = logging.getLogger("aims")

# Locate the HTML prototype (works both from source and frozen EXE)
def _find_frontend() -> Path | None:
    candidates = []
    import sys
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS))
    # AIMS_ROOT identifies the installation whose frontend.html the installer shipped;
    # without it a relocated/side-by-side tree would serve a stale UI from the repo.
    import os as _os
    if _os.environ.get("AIMS_ROOT"):
        candidates.append(Path(_os.environ["AIMS_ROOT"]).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent)  # project root
    candidates.append(Path.cwd())
    for base in candidates:
        for name in ("frontend.html", "AIMS_完整版.html", "index.html"):
            p = base / name
            if p.is_file():
                return p
        matches = sorted(base.glob("AIMS_*.html"))
        if matches:
            return matches[0]
    return None


FRONTEND_PATH = _find_frontend()


def _find_icon() -> Path | None:
    """assets/aims.ico (tab / window icon). Optional: Docker images may not ship it."""
    import sys
    candidates = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "assets")
        candidates.append(Path(sys.executable).parent / "assets")
    candidates.append(Path(__file__).resolve().parent.parent / "assets")
    for d in candidates:
        p = d / "aims.ico"
        if p.is_file():
            return p
    return None


ICON_PATH = _find_icon()

def _find_cores_dir() -> Path | None:
    import sys
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "cores" / "vendor")
        candidates.append(Path(sys.executable).parent / "vendor")
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "cores" / "vendor")
            candidates.append(Path(sys._MEIPASS) / "vendor")
    candidates.append(Path(__file__).resolve().parent.parent / "cores" / "vendor")
    candidates.append(Path.cwd() / "cores" / "vendor")
    for c in candidates:
        if c.is_dir():
            return c
    return None


CORES_DIR = _find_cores_dir()



@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: optionally auto-create tables, built-in roles and (if credentials were
    injected) the first admin. Demo data only when SEED_DEMO_DATA=true outside production."""
    try:
        import defusedxml  # noqa: F401  (openpyxl uses it to block XXE / billion-laughs in .xlsx)
    except ImportError:
        msg = "defusedxml is not installed: .xlsx parsing is not hardened against malicious XML"
        if settings.is_production:
            raise RuntimeError(msg)
        logger.warning(msg)
    if settings.AUTO_INIT_DB:
        try:
            from app.db.init_db import init_db
            init_db(seed=True)
            logger.info("Database initialized (AUTO_INIT_DB=true).")
            from app.db.session import SessionLocal
            from app.services.template_center import seed_builtin_templates

            with SessionLocal() as _s:
                seed_builtin_templates(_s)  # 官方内置模板（幂等）
        except RuntimeError:
            raise  # security-relevant startup failure (e.g. weak bootstrap password): do not run
        except Exception as e:
            logger.exception("Database auto-init failed: %s", e)
    try:
        from app.services.external_scheduler import (
            init_all_jobs,
            schedule_comm_stale_scan,
            schedule_retention,
        )
        init_all_jobs()
        # 审计项 F-6：通讯中断巡检以前没有任何调度者调用它，独立部署时永远
        # 不会自动执行。现在每 5 分钟自动巡检一次（阈值 15 分钟无数据=中断）。
        schedule_comm_stale_scan(interval_minutes=5, stale_minutes=15)
        # 审计项 D-1：数据保留清理（每日 03:30 UTC）+ 启动时立即补跑一次，
        # 让长时间不关机的机器也能追上清理进度。
        schedule_retention(hour=3)
    except Exception as e:
        logger.exception("External data scheduler init failed: %s", e)
    # 审计项 D-2：启动时回收崩溃后停留在 processing 的导入批次。
    try:
        from app.db.session import SessionLocal as _SL
        from app.services.retention import recover_orphan_imports

        with _SL() as _s:
            recover_orphan_imports(_s)
    except Exception as e:
        logger.exception("Orphan import recovery failed: %s", e)
    if FRONTEND_PATH:
        logger.info("Frontend prototype found: %s", FRONTEND_PATH)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description="Industrial Analyzer Smart O&M Platform — backend API",
    version=APP_VERSION,
    lifespan=lifespan,
    # API docs are off in production unless ENABLE_DOCS=true
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

# ---------------------------------------------------------------------------
# Middleware. `add_middleware` wraps the stack, so the LAST one added is the
# OUTERMOST. Effective order, outside -> in:
#   Metrics -> HTTPS enforcement -> Trusted host -> Security headers -> CORS
#   -> Audit -> Request size limit -> application
# ---------------------------------------------------------------------------
_API = settings.API_V1_PREFIX
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.MAX_UPLOAD_BYTES + 1024 * 1024)

if settings.AUDIT_ENABLED:
    app.add_middleware(
        AuditMiddleware,
        api_prefix=_API,
        writer=make_db_audit_writer(),
        # these endpoints write richer, explicit audit records themselves
        skip_paths={f"{_API}/auth/login", f"{_API}/auth/refresh", f"{_API}/auth/logout",
                    f"{_API}/auth/change-password"},
    )

# CORS: explicit whitelist only ("*" / "null" are rejected at config load). With an
# empty whitelist no CORS headers are emitted at all, i.e. cross-origin browsers are
# refused. The bundled UI (/ui) is same-origin and needs none.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["Retry-After", "Content-Disposition"],
        max_age=600,
    )

# 请求限流（M-7）：放在安全头内侧、CORS 外侧；只作用于 API 前缀，
# 登录/刷新用严格档，其余用宽松档。测试里通过 RATE_LIMIT_ENABLED=false 关闭。
if settings.RATE_LIMIT_ENABLED:
    app.add_middleware(
        RateLimitMiddleware,
        api_prefix=_API,
        auth_paths=("/auth/login", "/auth/refresh", "/auth/change-password"),
        global_limit=settings.RATE_LIMIT_API_PER_WINDOW,
        auth_limit=settings.RATE_LIMIT_AUTH_PER_WINDOW,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )

app.add_middleware(SecurityHeadersMiddleware, hsts=settings.https_required, api_prefix=_API)

if settings.allowed_host_list:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

if settings.https_required:
    app.add_middleware(HTTPSEnforcementMiddleware, exempt_paths=("/health",))

# 指标采集放在最外层：被安全头/限流/HTTPS 直接拒绝的请求也要计入（现场排障关心
# "哪一类请求被打回"）。见 /api/v1/system/metrics。
app.add_middleware(MetricsMiddleware, api_prefix=_API)


app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# Serve open-source analysis cores (SheetJS / Chart.js) downloaded at build time
if CORES_DIR is not None:
    app.mount("/cores", StaticFiles(directory=str(CORES_DIR)), name="cores")
    logger.info("Cores vendor mounted at /cores from %s", CORES_DIR)



@app.get("/health", tags=["System"])
def health_check():
    if settings.is_production:
        # no environment / filesystem details on an unauthenticated endpoint
        return {"status": "ok", "version": APP_VERSION}
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": APP_VERSION,
        "env": settings.APP_ENV,
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "frontend": str(FRONTEND_PATH) if FRONTEND_PATH else None,
        "cores": str(CORES_DIR) if CORES_DIR else None,
    }


@app.get("/", tags=["System"])
def root():
    body = {
        "message": f"{settings.APP_NAME} is running",
        "ui": "/ui" if FRONTEND_PATH else None,
        "api_prefix": settings.API_V1_PREFIX,
        "login": "POST /api/v1/auth/login",
    }
    if settings.docs_enabled:
        body["docs"] = "/docs"
    return body


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import JSONResponse
    if ICON_PATH is None:
        return JSONResponse(status_code=404, content={"detail": "no icon"})
    return FileResponse(ICON_PATH, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/ui", tags=["System"], include_in_schema=False)
def serve_ui():
    """Serve the AIMS HTML prototype for standalone / EXE mode."""
    if FRONTEND_PATH is None or not FRONTEND_PATH.is_file():
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=404,
            content={"detail": "Frontend HTML not found. Place AIMS_完整版.html next to the EXE."},
        )
    return FileResponse(FRONTEND_PATH, media_type="text/html")
