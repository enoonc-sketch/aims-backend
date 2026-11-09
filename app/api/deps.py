"""Shared FastAPI dependencies."""
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core import audit_context
from app.core.security import decode_access_token
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

__all__=["get_db","get_current_user","get_current_user_optional","get_current_superuser","has_permission","require_permission"]
