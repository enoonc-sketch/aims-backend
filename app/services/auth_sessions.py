"""Refresh-token session revocation helpers (shared by auth and user administration)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.system import RefreshToken


def revoke_family(db: Session, family_id: str, now: datetime) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_all_for_user(db: Session, user_id: int, now: datetime) -> int:
    res = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return res.rowcount or 0
