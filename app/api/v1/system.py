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
    _: User = Depends(get_current_user),
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
