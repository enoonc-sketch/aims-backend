"""Shared FastAPI dependencies."""
from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core import audit_context
from app.core.config import settings
from app.core.security import decode_access_token, verify_password
from app.deploy.conf import DEFAULT_ADMIN_PASSWORD, LEGACY_DEFAULT_ADMIN_PASSWORDS
from app.db.session import get_db
from app.models.system import User, UserRole, Role, RolePermission, Permission

security_scheme = HTTPBearer(auto_error=False)

def get_current_user_optional(db: Session = Depends(get_db), creds: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme)) -> Optional[User]:
    if creds is None or not creds.credentials: return None
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload: return None
    try: uid=int(payload["sub"])
    except (TypeError, ValueError): return None
    user=db.get(User, uid)
    if user and user.is_active:
        audit_context.note(user_id=user.id)
        return user
    return None

def get_current_user(user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated", headers={"WWW-Authenticate":"Bearer"})
    return user

def user_must_change_password(user: User) -> bool:
    """这个账号是否必须先改密码才能用其他功能（登录 / 刷新 / /auth/me 与服务端拦截共用同一判定）。

    两条来源取“或”：
      * 存着的标志：用默认口令建账号时打上（见 _bootstrap_admin，受
        DEV_ADMIN_FORCE_PASSWORD_CHANGE 开关控制），用户自己改密码后清零；
        别人替他重置密码不会清零——被别人给过密码的账号仍应自己改一次。
      * 现算的检查：开发管理员账号的口令哈希仍是内置默认口令。覆盖老库升级
        的情况——1.0.6 及更早的库没有这一列，管理员又一直没改过默认口令时，
        迁移只会补列（默认 False），没有这一条就漏拦了。
    开关关掉时两条都不触发；生产环境下默认账号本就不放行，也不触发。"""
    if not settings.DEV_ADMIN_FORCE_PASSWORD_CHANGE:
        return False
    if getattr(user, "must_change_password", False):
        return True
    if not settings.dev_admin_allowed:
        return False
    return (
        user.username == (settings.DEV_ADMIN_USERNAME.strip() or "admin")
        and any(verify_password(pw, user.hashed_password)
                for pw in (DEFAULT_ADMIN_PASSWORD, *LEGACY_DEFAULT_ADMIN_PASSWORDS))
    )

PASSWORD_CHANGE_REQUIRED = "password_change_required"
# 账号被要求先改密码时，仍然放行的接口（前缀 /api/v1 之后的路径）
_PWCHANGE_ALLOWED = ("/auth/me", "/auth/change-password", "/auth/logout", "/auth/refresh", "/auth/login", "/auth/ui-state")

def enforce_password_change(request: Request, user: Optional[User] = Depends(get_current_user_optional)) -> None:
    """全局依赖：默认口令账号必须先改密码，否则除 _PWCHANGE_ALLOWED 外的 /api 接口一律 403。
    服务端强制，不只靠前端界面拦。get_current_user_optional 在一次请求里有缓存，不会多查一次库。"""
    if user is None or not user_must_change_password(user):
        return
    path = request.url.path
    if not path.startswith("/api/"):
        return
    tail = path.split("/api/", 1)[1]
    tail = "/" + tail.split("/", 1)[1] if "/" in tail else "/"      # 去掉版本段 v1
    if tail.rstrip("/") in _PWCHANGE_ALLOWED:
        return
    raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED)

def get_current_superuser(user: User = Depends(get_current_user)) -> User:
    if not user.is_superuser: raise HTTPException(status_code=403, detail="Superuser required")
    return user

def has_permission(db: Session, user: User, code: str) -> bool:
    if user.is_superuser: return True
    stmt=(select(Permission.id).join(RolePermission, RolePermission.permission_id==Permission.id)
          .join(UserRole, UserRole.role_id==RolePermission.role_id)
          .where(UserRole.user_id==user.id, Permission.code==code))
    return db.execute(stmt).first() is not None

def require_permission(code: str):
    def dependency(db: Session=Depends(get_db), user: User=Depends(get_current_user)) -> User:
        if not has_permission(db,user,code):
            raise HTTPException(status_code=403, detail=f"Permission required: {code}")
        return user
    return dependency

__all__=["get_db","get_current_user","user_must_change_password","enforce_password_change","PASSWORD_CHANGE_REQUIRED","get_current_user_optional","get_current_superuser","has_permission","require_permission"]
