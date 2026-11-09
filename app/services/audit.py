"""Audit trail helpers.

Two layers feed the `audit_logs` table:
  1. Explicit events written with `record_audit()` (login success/failure/lockout,
     refresh, refresh-reuse, logout, password change, user administration, ...).
  2. `AuditMiddleware` (app/core/audit_middleware.py), which records every
     state-changing API call (POST/PUT/PATCH/DELETE) with actor, IP, status and
     — for uploads — file name / size / sha256, so nothing slips through even if
     an endpoint forgets to log.

Request bodies and credentials are never written to the audit trail.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.system import AuditLog

_MAX_DETAIL_VALUE = 500


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_DETAIL_VALUE:
        return value[:_MAX_DETAIL_VALUE] + "…"
    return value


def record_audit(
    db: Session,
    *,
    user_id: int | None,
    action: str,
    entity: str | None = None,
    entity_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Add an audit row to `db` (the caller commits, so the record is atomic with
    the change it describes)."""
    row = AuditLog(
        user_id=user_id,
        action=action[:100],
        entity=entity[:100] if entity else None,
        entity_id=str(entity_id)[:50] if entity_id is not None else None,
        detail={k: _clip(v) for k, v in (detail or {}).items()},
        ip_address=ip_address[:64] if ip_address else None,
    )
    db.add(row)
    return row
