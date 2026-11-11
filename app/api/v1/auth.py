"""Auth API — login (with failure throttling), token refresh (rotating), logout,
password change, current-user profile.

Security behaviour
------------------
* Login failures are counted per (lower-cased) username and per client IP inside a
  sliding window. Once a limit is hit the API answers 429 + Retry-After *without
  evaluating the password*, so a correct guess during lockout does not help.
  Unknown usernames are counted exactly like known ones (no enumeration) and cost
  the same CPU time (dummy bcrypt check).
* Access tokens are short lived JWTs. Long sessions use opaque refresh tokens that
  are single-use: each refresh revokes the presented token and issues a new one in
  the same family. Re-presenting a revoked token revokes the whole family (theft
  detection). Only keyed digests of refresh tokens are stored.
* Every security-relevant event is written to `audit_logs`: login success/failure/
  blocked, refresh (+ invalid/expired/reuse), logout, password change.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_optional, get_db
from app.core.config import settings
from app.core.security import (
    burn_password_check,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    validate_password_strength,
    verify_password,
)
from app.models.system import LoginFailure, RefreshToken, User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
    UserOut,
)
from app.services.audit import record_audit
from app.services.auth_sessions import revoke_all_for_user, revoke_family

router = APIRouter(prefix="/auth", tags=["Auth"])

_GENERIC_LOGIN_ERROR = "Incorrect username or password"


# ----------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything we store is UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:255]


def _username_key(username: str) -> str:
    return username.strip().lower()[:100]


def _lock_status(db: Session, username_key: str, ip: Optional[str], now: datetime) -> Optional[int]:
    """Return seconds until the next attempt is allowed, or None if not locked."""
    window = timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    since = now - window
    retry_candidates: list[datetime] = []

    def _check(column, value, limit: int) -> None:
        stamps = db.scalars(
            select(LoginFailure.created_at)
            .where(column == value, LoginFailure.created_at >= since)
            .order_by(LoginFailure.created_at.asc())
        ).all()
        if len(stamps) >= limit:
            # the lock lifts when enough old failures have aged out of the window
            retry_candidates.append(_aware(stamps[len(stamps) - limit]) + window)

    _check(LoginFailure.username, username_key, settings.LOGIN_MAX_FAILURES)
    if ip:
        _check(LoginFailure.ip_address, ip, settings.LOGIN_MAX_FAILURES_PER_IP)
    if not retry_candidates:
        return None
    return max(1, int((max(retry_candidates) - now).total_seconds()) + 1)


def _register_failure(db: Session, username_key: str, ip: Optional[str]) -> None:
    db.add(LoginFailure(username=username_key, ip_address=ip))


def _purge_stale(db: Session, now: datetime) -> None:
    db.execute(delete(LoginFailure).where(LoginFailure.created_at < now - timedelta(days=1)))
    db.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=30)))


def _blocked(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many failed attempts. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def _issue_tokens(db: Session, user: User, family_id: str, request: Request, now: datetime) -> TokenResponse:
    refresh_plain = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_plain),
            family_id=family_id,
            expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    )
    return TokenResponse(
        access_token=create_access_token(user.id, extra={"username": user.username}),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        refresh_token=refresh_plain,
        refresh_expires_in=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )


# ----------------------------------------------------------------------------- endpoints
@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    now = _now()
    ip = _client_ip(request)
    key = _username_key(payload.username)
    _purge_stale(db, now)

    retry_after = _lock_status(db, key, ip, now)
    if retry_after is not None:
        record_audit(db, user_id=None, action="AUTH_LOGIN_BLOCKED", entity="auth",
                     detail={"username": key, "retry_after": retry_after}, ip_address=ip)
        db.commit()
        raise _blocked(retry_after)

    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None:
        burn_password_check(payload.password)  # equalise timing with the real-user path
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.hashed_password)

    if not password_ok:
        _register_failure(db, key, ip)
        record_audit(db, user_id=user.id if user else None, action="AUTH_LOGIN_FAILED", entity="auth",
                     detail={"username": key}, ip_address=ip)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_LOGIN_ERROR)

    if not user.is_active:
        record_audit(db, user_id=user.id, action="AUTH_LOGIN_DISABLED_ACCOUNT", entity="auth",
                     detail={"username": key}, ip_address=ip)
        db.commit()
        raise HTTPException(status_code=403, detail="User is disabled")

    db.execute(delete(LoginFailure).where(LoginFailure.username == key))
    user.last_login_at = now
    tokens = _issue_tokens(db, user, family_id=uuid4().hex, request=request, now=now)
    record_audit(db, user_id=user.id, action="AUTH_LOGIN_SUCCESS", entity="auth",
                 detail={"username": user.username}, ip_address=ip)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    now = _now()
    ip = _client_ip(request)
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(payload.refresh_token)))
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token",
                            headers={"WWW-Authenticate": "Bearer"})

    if row is None:
        record_audit(db, user_id=None, action="AUTH_REFRESH_INVALID", entity="auth", ip_address=ip)
        db.commit()
        raise invalid

    if row.revoked_at is not None:
        # A rotated/revoked token came back: either a replay or a stolen copy. Kill the family.
        revoke_family(db, row.family_id, now)
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_REUSE_DETECTED", entity="auth",
                     detail={"family": row.family_id}, ip_address=ip)
        db.commit()
        raise invalid

    if _aware(row.expires_at) <= now:
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_EXPIRED", entity="auth", ip_address=ip)
        db.commit()
        raise invalid

    # 审计项 D-6：单次使用改为条件 UPDATE——两个并发请求用同一个 refresh token
    # 到达时，只有把 revoked_at 从 NULL 改成 now 的那一个能继续，另一个按
    # "重放"处理（整族吊销）。原来的"读-判-写"存在竞态窗口。
    consumed = db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if consumed.rowcount == 0:
        revoke_family(db, row.family_id, now)
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_REUSE_DETECTED", entity="auth",
                     detail={"family": row.family_id, "reason": "concurrent"}, ip_address=ip)
        db.commit()
        raise invalid

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        revoke_family(db, row.family_id, now)
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_DENIED_INACTIVE", entity="auth", ip_address=ip)
        db.commit()
        raise invalid

    tokens = _issue_tokens(db, user, family_id=row.family_id, request=request, now=now)
    record_audit(db, user_id=user.id, action="AUTH_REFRESH", entity="auth", ip_address=ip)
    db.commit()
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    payload: Optional[LogoutRequest] = None,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """With a refresh token: end that session (its whole token family). With only a
    valid access token: end every session of the user."""
    now = _now()
    ip = _client_ip(request)
    user_id: Optional[int] = None
    revoked = 0
    presented = payload.refresh_token if payload else None

    if presented:
        row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(presented)))
        if row is not None:
            user_id = row.user_id
            revoke_family(db, row.family_id, now)
            revoked = 1
    elif user is not None:
        user_id = user.id
        revoked = revoke_all_for_user(db, user.id, now)
    else:
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})

    record_audit(db, user_id=user_id, action="AUTH_LOGOUT", entity="auth",
                 detail={"sessions_revoked": revoked}, ip_address=ip)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", response_model=TokenResponse)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change the caller's password. All refresh tokens are revoked (other devices are
    signed out); fresh tokens are returned for the current session. Access tokens
    already issued stay valid until they expire (<= ACCESS_TOKEN_EXPIRE_MINUTES)."""
    now = _now()
    ip = _client_ip(request)
    key = _username_key(user.username)

    retry_after = _lock_status(db, key, ip, now)
    if retry_after is not None:
        raise _blocked(retry_after)

    if not verify_password(payload.current_password, user.hashed_password):
        _register_failure(db, key, ip)  # a stolen access token must not allow password guessing
        record_audit(db, user_id=user.id, action="AUTH_PASSWORD_CHANGE_FAILED", entity="auth", ip_address=ip)
        db.commit()
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    try:
        validate_password_strength(payload.new_password, user.username)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if verify_password(payload.new_password, user.hashed_password):
        raise HTTPException(status_code=422, detail="New password must differ from the current one")

    user.hashed_password = hash_password(payload.new_password)
    revoke_all_for_user(db, user.id, now)
    tokens = _issue_tokens(db, user, family_id=uuid4().hex, request=request, now=now)
    record_audit(db, user_id=user.id, action="AUTH_PASSWORD_CHANGE", entity="users", entity_id=user.id, ip_address=ip)
    db.commit()
    return tokens


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
