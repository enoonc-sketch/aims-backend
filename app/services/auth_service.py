"""Auth helpers for the CLI tools (setup wizard / user management).

.. note::
   This module is a **compatibility adapter**, not the consolidated
   ``auth_service`` implementation from the 0.3.0-based ``secure`` package.
   That package kept throttling, refresh-token rotation, audit and admin
   creation in one module; 0.5.0 implements the same behaviour split across
   :mod:`app.core.security`, :mod:`app.services.auth_sessions`,
   :mod:`app.services.audit` and :mod:`app.db.init_db` (see
   ``CHANGES_MERGE_0_5_2.md``). The functions here expose the small surface
   the setup wizard (``app.cli.setup``) and the user-management CLI
   (``app.cli.manage_users``) need, implemented on top of the 0.5.0 modules
   so the wizard code can be kept verbatim.

   Behaviour notes (matching 0.5.0, not the secure package):

   * No ``token_version`` column exists on ``User`` in 0.5.0: access tokens
     are short-lived JWTs and expire by TTL. ``invalidate_user_sessions``
     therefore revokes all refresh tokens (same as the ``reset-password``
     CLI command); outstanding access tokens live out their remaining
     minutes instead of being invalidated instantly.
   * ``create_admin_user`` also seeds permissions, like 0.5.0's own
     ``create-admin`` CLI command does; the secure package only seeded roles.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.system import Role, User, UserRole

logger = logging.getLogger(__name__)


def write_audit(
    db: Session, *, action: str, user_id: Optional[int] = None, entity: Optional[str] = None,
    entity_id: Optional[str | int] = None, detail: Optional[dict[str, Any]] = None, ip: Optional[str] = None,
) -> None:
    """Write one audit row (no commit). An audit failure must never break the
    business operation, but it must leave an error log behind."""
    if not settings.AUDIT_ENABLED:
        return
    from app.services.audit import record_audit

    try:
        with db.begin_nested():
            record_audit(
                db, user_id=user_id, action=action, entity=entity,
                entity_id=entity_id, detail=detail, ip_address=ip,
            )
    except Exception:  # noqa: BLE001
        logger.exception("audit write failed for action=%s", action)


def has_any_user(db: Session) -> bool:
    return db.scalar(select(User.id).limit(1)) is not None


def create_admin_user(db: Session, username: str, password: str, *, audit_action: str = "cli.admin_created") -> User:
    """Create a superuser (no commit). Password must pass the policy;
    violations raise ValueError with a user-facing message."""
    from app.core.security import hash_password, validate_password_strength
    from app.db.init_db import _seed_permissions, _seed_roles

    username = (username or "").strip()
    if len(username) < 3:
        raise ValueError("用户名至少 3 个字符")
    if db.scalar(select(User).where(User.username == username)) is not None:
        raise ValueError("用户已存在")
    try:
        validate_password_strength(password, username)
    except ValueError as e:
        # 0.5.0 raises; the secure package returned a problem list.
        raise ValueError(f"密码不符合策略: {e}") from e
    _seed_roles(db)
    _seed_permissions(db)
    user = User(username=username, hashed_password=hash_password(password), full_name="Administrator",
                is_superuser=True, is_active=True)
    db.add(user)
    db.flush()
    role = db.scalar(select(Role).where(Role.code == "admin"))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    write_audit(db, action=audit_action, entity="users", entity_id=user.id, detail={"username": username})
    return user


def invalidate_user_sessions(db: Session, user: User, reason: str) -> None:
    """Revoke all of the user's refresh tokens (access tokens expire by TTL).
    Mirrors the ``reset-password`` CLI command in 0.5.0."""
    from app.services.auth_sessions import revoke_all_for_user

    revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
    logger.info("invalidated sessions for user_id=%s reason=%s", user.id, reason)
