"""System API — users, roles, settings, audit logs (read-focused)."""
from typing import Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_superuser, get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.core.security import hash_password
from app.models.system import AuditLog, Permission, Role, RolePermission, SystemSetting, User, UserRole
from app.services.audit import record_audit
from app.services.auth_sessions import revoke_all_for_user
from app.services import access
from app.schemas.auth import (
    PermissionOut,
    RoleCreate,
    RolePermissionsOut,
    RolePermissionsUpdate,
    UserRolesUpdate,
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
    users = db.scalars(select(User).order_by(User.id).offset(skip).limit(limit)).all()
    roles = access.role_codes_for(db, [u.id for u in users])
    out = []
    for u in users:
        item = UserOut.model_validate(u)
        item.roles = roles.get(u.id, [])
        out.append(item)
    return out


def _roles_by_code(db: Session, codes: list[str]) -> list[Role]:
    codes = list(dict.fromkeys(c.strip() for c in codes if c and c.strip()))
    rows = db.scalars(select(Role).where(Role.code.in_(codes))).all() if codes else []
    missing = sorted(set(codes) - {r.code for r in rows})
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown role: {', '.join(missing)}")
    return list(rows)


def _set_user_roles(db: Session, user: User, roles: list[Role]) -> None:
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for r in roles:
        db.add(UserRole(user_id=user.id, role_id=r.id))


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="username already exists")
    roles = _roles_by_code(db, payload.roles)
    user = User(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        email=payload.email,
        is_superuser=payload.is_superuser,
        # 管理员代建的账号：建账号的人知道口令，账号本人首次登录必须先改成自己的口令。
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    _set_user_roles(db, user, roles)
    record_audit(db, user_id=actor.id, action="USER_CREATE", entity="users", entity_id=user.id,
                 detail={"username": user.username, "is_superuser": user.is_superuser,
                         "roles": [r.code for r in roles]},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    out = UserOut.model_validate(user)
    out.roles = [r.code for r in roles]
    return out


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
    # 别人替他重置了密码：该账号下次登录必须先改成自己的口令（自己在管理界面改自己的
    # 密码不触发——新口令是本人当场设的）。本人走 /auth/change-password 改密时会清零。
    if password_reset and actor.id != user.id:
        user.must_change_password = True
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


@router.put("/users/{user_id}/roles", response_model=UserOut)
def set_user_roles(
    user_id: int,
    payload: UserRolesUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    """给账号分配角色（整体替换）。"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    roles = _roles_by_code(db, payload.roles)
    _set_user_roles(db, user, roles)
    record_audit(db, user_id=actor.id, action="USER_ROLES_SET", entity="users", entity_id=user.id,
                 detail={"roles": [r.code for r in roles]},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    out = UserOut.model_validate(user)
    out.roles = [r.code for r in roles]
    return out


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    """删除账号。不能删自己，也不能删掉最后一个超级管理员（否则无人可管）。"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if user.is_superuser:
        supers = db.scalar(select(func.count()).select_from(User).where(User.is_superuser.is_(True)))
        if (supers or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last superuser")
    revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    record_audit(db, user_id=actor.id, action="USER_DELETE", entity="users", entity_id=user.id,
                 detail={"username": user.username},
                 ip_address=request.client.host if request.client else None)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Roles ----------
@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.scalars(select(Role)).all()


@router.get("/permissions", response_model=list[PermissionOut])
def list_permissions(db: Session = Depends(get_db), _: User = Depends(get_current_superuser)):
    return db.scalars(select(Permission).order_by(Permission.module, Permission.code)).all()


@router.get("/role-permissions", response_model=list[RolePermissionsOut])
def list_role_permissions(db: Session = Depends(get_db), _: User = Depends(get_current_superuser)):
    roles = db.scalars(select(Role).order_by(Role.id)).all()
    pairs = db.execute(select(RolePermission.role_id, Permission.code)
                       .join(Permission, Permission.id == RolePermission.permission_id)).all()
    by_role: dict[int, list[str]] = {}
    for rid, code in pairs:
        by_role.setdefault(rid, []).append(code)
    return [RolePermissionsOut(id=r.id, code=r.code, name=r.name, description=r.description,
                               permissions=sorted(by_role.get(r.id, []))) for r in roles]


@router.put("/roles/{role_id}/permissions", response_model=RolePermissionsOut)
def set_role_permissions(
    role_id: int,
    payload: RolePermissionsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    """整体替换某个角色的权限集合。admin 角色恒为全部权限，不可改。"""
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.code == "admin":
        raise HTTPException(status_code=400, detail="The admin role always has every permission")
    codes = list(dict.fromkeys(payload.permissions))
    perms = db.scalars(select(Permission).where(Permission.code.in_(codes))).all() if codes else []
    missing = sorted(set(codes) - {p.code for p in perms})
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown permission: {', '.join(missing)}")
    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    for p in perms:
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    record_audit(db, user_id=actor.id, action="ROLE_PERMISSIONS_SET", entity="roles", entity_id=role.id,
                 detail={"role": role.code, "permissions": sorted(codes)},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    return RolePermissionsOut(id=role.id, code=role.code, name=role.name, description=role.description,
                              permissions=sorted(codes))


@router.post("/roles", response_model=RoleOut, status_code=201)
def create_role(
    payload: RoleCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    """新建自定义角色。code 只允许小写字母、数字、下划线；新建角色初始没有任何权限，
    建完后在权限设置里勾选。"""
    if db.scalar(select(Role).where(Role.code == payload.code)):
        raise HTTPException(status_code=409, detail="role already exists")
    role = Role(code=payload.code, name=payload.name, description=payload.description)
    db.add(role)
    db.flush()
    record_audit(db, user_id=actor.id, action="ROLE_CREATE", entity="roles", entity_id=role.id,
                 detail={"code": role.code, "name": role.name},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(role)
    return role


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_superuser),
):
    """删除自定义角色。内置 admin 角色不可删；还有账号在用的角色不可删（先把账号移走）。"""
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.code == "admin":
        raise HTTPException(status_code=400, detail="The admin role cannot be deleted")
    assigned = db.scalar(select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id))
    if assigned:
        raise HTTPException(status_code=400,
                            detail=f"Role is assigned to {assigned} user(s); reassign them first")
    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    record_audit(db, user_id=actor.id, action="ROLE_DELETE", entity="roles", entity_id=role.id,
                 detail={"code": role.code},
                 ip_address=request.client.host if request.client else None)
    db.delete(role)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
# 目录占用统计缓存：Models（多 GB 的 GGUF）/Backup/Database 递归 stat 一遍在真实
# 安装上要数秒，而系统设置页每次切到 Components 页签都会请求。带 TTL 缓存既避免
# 反复扫描，也避免超过前端 4 秒兜底超时后被掐断（表现为莫名其妙的加载失败）。
_DIR_SIZE_TTL_SECONDS = 300.0
_DIR_SIZE_CACHE: dict[str, tuple[float, float | None]] = {}
_LLAMA_ENGINE_INFO: tuple | None = None


@router.get("/components")
def list_components(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    import platform as _platform
    import sys as _sys
    import time as _time
    from pathlib import Path

    from app.deploy import APP_VERSION
    from app.core.config import settings
    from sqlalchemy import text as _text

    def _dir_size_mb(p) -> float | None:
        key = str(p)
        now = _time.monotonic()
        hit = _DIR_SIZE_CACHE.get(key)
        if hit is not None and now - hit[0] < _DIR_SIZE_TTL_SECONDS:
            return hit[1]
        try:
            base = Path(p)
            if not base.exists():
                value = None
            else:
                total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
                value = round(total / (1024 * 1024), 1)
        except Exception:
            value = None
        _DIR_SIZE_CACHE[key] = (now, value)
        return value

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

    # AI 引擎（llama.cpp；未安装时如实报告）。import llama_cpp 在打包 EXE 里要
    # 加载整个推理 DLL（秒级），每次点开页签都来一遍不可接受——结果进程级缓存。
    global _LLAMA_ENGINE_INFO  # noqa: F824
    if _LLAMA_ENGINE_INFO is None:
        try:
            import llama_cpp as _llama
            _LLAMA_ENGINE_INFO = (getattr(_llama, "__version__", None),
                                  str(Path(_llama.__file__).parent), "installed")
        except Exception:
            _LLAMA_ENGINE_INFO = (None, None, "not-installed")
    engine_version, engine_path, engine_status = _LLAMA_ENGINE_INFO
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

    # 目录占用汇总（模型/日志/备份）。注意：Settings 里没有 AIMS_DATA_ROOT 字段，
    # 原来的 getattr(settings, ...) 恒为 None——这个功能静默失效了很久。数据根
    # 以部署布局为准（AIMS_DATA_ROOT 环境变量 / 安装布局），源码运行时是仓库就地目录。
    try:
        from app.deploy.paths import load_layout as _load_layout

        root = _load_layout().base
        usage = {
            "models_mb": _dir_size_mb(root / "Models"),
            "logs_mb": _dir_size_mb(root / "Logs"),
            "backup_mb": _dir_size_mb(root / "Backup"),
            "database_mb": _dir_size_mb(root / "Database"),
        }
    except Exception:
        usage = {}

    return {"app_version": APP_VERSION, "python_version": _sys.version.split()[0],
            "platform": f"{_platform.system()} {_platform.release()}",
            "components": components, "disk_usage": usage}


# ---------- 运行指标（总方案 8 章"指标导出"）----------
@router.get("/metrics")
def runtime_metrics(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_superuser),
    format: str = Query("json", pattern="^(json|prometheus)$",
                        description="json: 结构化快照；prometheus: 文本抓取格式"),
):
    """当前进程的运行指标快照 / Prometheus 抓取端点。

    仅超管可读：内容含进程 ID、内存、线程数、路由分布，属运维数据，普通业务用户
    不需要也不应该看到（同一套 RBAC 原则，见 /system/users）。

    指标来自进程内计数器（`app/core/metrics.py`），重启即清零——单机部署下这是
    可接受的，也让"重启后指标异常"本身成为一条有用的信号。数据库连接池状态在
    PostgreSQL + QueuePool 下如实返回，SQLite/NullPool 返回 null。
    """
    from fastapi.responses import PlainTextResponse

    from app.core.metrics import REGISTRY

    if format == "prometheus":
        return PlainTextResponse(REGISTRY.prometheus(),
                                 headers={"Cache-Control": "no-store"})

    snap = REGISTRY.snapshot()
    pool_status = None
    try:
        pool = db.get_bind().pool
        pool_status = {"class": type(pool).__name__}
        if hasattr(pool, "status"):
            pool_status["status"] = pool.status()
        if hasattr(pool, "size") and callable(pool.size):
            pool_status.update({"size": pool.size(), "checkedout": pool.checkedout(),
                                "overflow": pool.overflow()})
    except Exception:
        pool_status = None
    snap["database"] = {"dialect": db.get_bind().dialect.name, "pool": pool_status}
    return snap
