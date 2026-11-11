"""System API — users, roles, settings, audit logs (read-focused)."""
from typing import Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_superuser, get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.core.security import hash_password
from app.models.system import AuditLog, Role, SystemSetting, User
from app.services.audit import record_audit
from app.services.auth_sessions import revoke_all_for_user
from app.schemas.auth import (
    RoleOut,
    SystemSettingOut,
    SystemSettingUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/system", tags=["System"])


# ---------- Users ----------
@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    # 用户清单含邮箱、超管标志、最后登录时间——属于管理数据，普通用户不得枚举。
    _: User = Depends(get_current_superuser),
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    return db.scalars(select(User).offset(skip).limit(limit)).all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="username already exists")
    user = User(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        email=payload.email,
        is_superuser=payload.is_superuser,
    )
    db.add(user)
    db.flush()
    record_audit(db, user_id=actor.id, action="USER_CREATE", entity="users", entity_id=user.id,
                 detail={"username": user.username, "is_superuser": user.is_superuser},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    data = payload.model_dump(exclude_unset=True)
    password_reset = bool(data.get("password"))
    if password_reset:
        data["hashed_password"] = hash_password(data.pop("password"))
    else:
        data.pop("password", None)
    for k, v in data.items():
        setattr(user, k, v)
    # Deactivating an account or resetting its password ends all of its sessions.
    sessions_revoked = 0
    if password_reset or data.get("is_active") is False:
        sessions_revoked = revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
    record_audit(db, user_id=actor.id, action="USER_UPDATE", entity="users", entity_id=user.id,
                 detail={"fields": sorted(k for k in data if k != "hashed_password"),
                         "password_reset": password_reset, "sessions_revoked": sessions_revoked},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    return user


# ---------- Roles ----------
@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.scalars(select(Role)).all()


# ---------- Settings ----------
@router.get("/settings", response_model=list[SystemSettingOut])
def list_settings(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.scalars(select(SystemSetting)).all()


@router.get("/settings/{key}", response_model=SystemSettingOut)
def get_setting(key: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not row:
        raise HTTPException(status_code=404, detail="Setting not found")
    return row


@router.put("/settings/{key}", response_model=SystemSettingOut)
def upsert_setting(
    key: str,
    payload: SystemSettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is None:
        row = SystemSetting(key=key, value=payload.value, description=payload.description)
        db.add(row)
    else:
        if payload.value is not None:
            row.value = payload.value
        if payload.description is not None:
            row.description = payload.description
    record_audit(db, user_id=actor.id, action="SETTING_UPSERT", entity="system_settings", entity_id=key,
                 detail={"key": key},  # values are not logged: settings may hold sensitive data
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(row)
    return row


# ---------- Audit logs (read-only) ----------
@router.get("/audit-logs")
def list_audit_logs(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_superuser),
    skip: int = SkipQuery,
    limit: int = LimitQuery(50),
    action: Optional[str] = Query(None, max_length=100, description="Exact action, e.g. AUTH_LOGIN_FAILED"),
    user_id: Optional[int] = Query(None),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset(skip).limit(limit)).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "action": r.action,
            "entity": r.entity,
            "entity_id": r.entity_id,
            "detail": r.detail,
            "ip_address": r.ip_address,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 统一后台组件清单（AIMS 1.0 阶段 3-5 补齐，总方案第八部分 8.1）
#
# 聚合各组件的名称/版本/架构/路径/运行状态，供前端"关于 / 组件中心"页展示。
# 数据全部真实采集：Python Runtime 来自 sys，数据库来自当前连接与 SHOW，
# pgvector 来自 pg_extension，前端/文档解析组件来自磁盘文件探测，AI 引擎来自
# llama_cpp 与模型目录。不支持的指标明确返回 null，不伪造数值。
# ---------------------------------------------------------------------------
@router.get("/components")
def list_components(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    import platform as _platform
    import sys as _sys
    from pathlib import Path

    from app.deploy import APP_VERSION
    from app.core.config import settings
    from sqlalchemy import text as _text

    def _dir_size_mb(p) -> float | None:
        try:
            base = Path(p)
            if not base.exists():
                return None
            total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
            return round(total / (1024 * 1024), 1)
        except Exception:
            return None

    components: list[dict] = []

    components.append({
        "name": "Backend (FastAPI)",
        "category": "service",
        "version": APP_VERSION,
        "arch": _platform.machine(),
        "path": str(Path(__file__).resolve().parents[2]),
        "status": "running",
        "license": "proprietary",
        "python": _sys.version.split()[0],
    })

    db_size = None
    pg_version = None
    vector_enabled = False
    try:
        pg_version = db.execute(_text("SELECT version()")).scalar()
        if pg_version:
            pg_version = pg_version.split("on ")[0].strip()
        db_size = db.execute(_text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()
        vector_enabled = db.execute(
            _text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
        ).scalar() is True
    except Exception:
        pg_version = None  # SQLite 等非 PG 后端
    components.append({
        "name": "PostgreSQL",
        "category": "database",
        "version": pg_version,
        "arch": None,
        "path": None,
        "status": "running" if pg_version else "not-applicable",
        "license": "PostgreSQL Licence",
        "database_size": db_size,
    })
    components.append({
        "name": "pgvector",
        "category": "extension",
        "version": None,
        "arch": None,
        "path": None,
        "status": "enabled" if vector_enabled else "not-installed",
        "license": "PostgreSQL Licence",
    })

    # AI 引擎（llama.cpp；未安装时如实报告）
    engine_version = None
    engine_path = None
    try:
        import llama_cpp
        engine_version = getattr(llama_cpp, "__version__", None)
        engine_path = str(Path(llama_cpp.__file__).parent)
        engine_status = "installed"
    except Exception:
        engine_status = "not-installed"
    components.append({
        "name": "AI Engine (llama.cpp)",
        "category": "service",
        "version": engine_version,
        "arch": None,
        "path": engine_path,
        "status": engine_status,
        "license": "MIT",
    })

    # 前端与文档解析组件
    frontend = Path(__file__).resolve().parents[2] / "frontend.html"
    components.append({
        "name": "Frontend (单文件应用)",
        "category": "resource",
        "version": None,
        "arch": None,
        "path": str(frontend) if frontend.exists() else None,
        "status": "installed" if frontend.exists() else "missing",
        "license": "proprietary",
    })

    # 目录占用汇总（模型/日志/备份）
    data_root = getattr(settings, "AIMS_DATA_ROOT", None)
    usage = {}
    if data_root:
        from pathlib import Path as _P
        root = _P(str(data_root))
        usage = {
            "models_mb": _dir_size_mb(root / "Models"),
            "logs_mb": _dir_size_mb(root / "Logs"),
            "backup_mb": _dir_size_mb(root / "Backup"),
            "database_mb": _dir_size_mb(root / "Database"),
        }

    return {"app_version": APP_VERSION, "python_version": _sys.version.split()[0],
            "platform": f"{_platform.system()} {_platform.release()}",
            "components": components, "disk_usage": usage}
