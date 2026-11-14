"""System domain models: users, roles, permissions, settings, audit logs (7 tables)."""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, JSON, false, func
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100))
    email = Column(String(150))
    is_active = Column(Boolean, nullable=False, default=True)
    is_superuser = Column(Boolean, nullable=False, default=False)
    # 用默认口令（admin/admin123）建出来的账号：必须先改密码才能用其他接口，改完清零。
    must_change_password = Column(Boolean, nullable=False, default=False, server_default=false())
    last_login_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True)
    code = Column(String(100), unique=True, nullable=False)
    name = Column(String(150), nullable=False)
    module = Column(String(50))


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id = Column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_user_action", "user_id", "action"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    action = Column(String(100), nullable=False)
    entity = Column(String(100))
    entity_id = Column(String(50))
    detail = Column(JSON)
    ip_address = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    """Server-side record of an issued refresh token.

    Only a keyed digest of the token is stored (never the token itself). Tokens are
    single-use: every refresh revokes the presented token and issues a new one in
    the same `family_id`. Presenting an already-revoked token is treated as theft
    and revokes the whole family.
    """
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("idx_refresh_tokens_user", "user_id"),
        Index("idx_refresh_tokens_family", "family_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    family_id = Column(String(32), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ip_address = Column(String(64))
    user_agent = Column(String(255))


class LoginFailure(Base):
    """One row per failed login attempt; drives per-account and per-IP throttling.
    Rows for an account are cleared after a successful login and old rows are
    purged opportunistically. The permanent trail lives in `audit_logs`."""
    __tablename__ = "login_failures"
    __table_args__ = (
        Index("idx_login_failures_user_time", "username", "created_at"),
        Index("idx_login_failures_ip_time", "ip_address", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    username = Column(String(100), nullable=False)
    ip_address = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
