"""Auth API — login (with failure throttling), token refresh (rotating), logout,
password change, current-user profile.

Security behaviour
------------------
* Login failures are counted per (client IP, lower-cased username) PAIR inside a
  sliding window, plus a much higher per-IP ceiling against username spraying.
  Once a limit is hit the API answers 429 + Retry-After *without evaluating the
  password*, so a correct guess during lockout does not help. Because the pair is
  the key, someone typing wrong passwords for `admin` from their own address locks
  only themselves out of `admin`; the real administrator on another address (and
  other usernames on the same address) are unaffected.
  Unknown usernames are counted exactly like known ones (no enumeration) and cost
  the same CPU time (dummy bcrypt check).
* Access tokens are short lived JWTs. Long sessions use opaque refresh tokens that
  are single-use: each refresh revokes the presented token and issues a new one in
  the same family. Re-presenting a revoked token revokes the whole family (theft
  detection). Only keyed digests of refresh tokens are stored.
* Login, refresh and /auth/me report `must_change_password` when the account still
  uses the built-in default administrator password (see user_must_change_password
  in app.api.deps: a flag set at bootstrap, or the stored hash still being the
  default password): the frontend then sends the user to change it before any
  other feature can be used, and the server itself refuses other API calls
  until then. The password policy never accepts that password as a new one, so
  the flag clears itself the moment the password is changed.
* Every security-relevant event is written to `audit_logs`: login success/failure/
  blocked, refresh (+ invalid/expired/reuse), logout, password change.
"""
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_optional, get_db, security_scheme, user_must_change_password
from app.services import access, tray_summary
from app.core.config import settings
from app.core.security import (
    decode_access_token,
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
    UserSelfUpdate,
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


def _refresh_grace_seconds() -> int:
    """并发刷新的宽容窗口（秒）。AIMS_REFRESH_GRACE_SECONDS=0 可关闭。"""
    import os

    raw = os.environ.get("AIMS_REFRESH_GRACE_SECONDS", "5")
    try:
        return max(0, int(raw))
    except ValueError:
        return 5


def _revoked_within_grace(revoked_at: Optional[datetime], now: datetime) -> bool:
    """令牌是否在宽容窗口内被消费（用于区分"真并发"与"事后重放"）。

    用绝对值差：赢家的消费时间戳可能比输家的 now 早/晚几个微秒（时钟采样
    顺序不定），只看正向 age 会把真并发误判成重放。
    """
    grace = _refresh_grace_seconds()
    if grace <= 0 or revoked_at is None:
        return False
    age = abs((now - _aware(revoked_at)).total_seconds())
    return age <= grace


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:255]


def _username_key(username: str) -> str:
    return username.strip().lower()[:100]


def _ip_clause(ip: Optional[str]):
    return LoginFailure.ip_address.is_(None) if ip is None else LoginFailure.ip_address == ip


def _lock_status(db: Session, username_key: str, ip: Optional[str], now: datetime) -> Optional[int]:
    """Return seconds until the next attempt is allowed, or None if not locked.

    Two independent counters:
      * (ip, username) pair  -> LOGIN_MAX_FAILURES   (locks only that pair)
      * ip alone             -> LOGIN_MAX_FAILURES_PER_IP (username-spraying ceiling)
    A failure from another address never counts against this address's pair."""
    window = timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    since = now - window
    retry_candidates: list[datetime] = []

    def _check(clauses: list, limit: int) -> None:
        stamps = db.scalars(
            select(LoginFailure.created_at)
            .where(*clauses, LoginFailure.created_at >= since)
            .order_by(LoginFailure.created_at.asc())
        ).all()
        if len(stamps) >= limit:
            # the lock lifts when enough old failures have aged out of the window
            retry_candidates.append(_aware(stamps[len(stamps) - limit]) + window)

    _check([LoginFailure.username == username_key, _ip_clause(ip)], settings.LOGIN_MAX_FAILURES)
    if ip:
        _check([LoginFailure.ip_address == ip], settings.LOGIN_MAX_FAILURES_PER_IP)
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


def _issue_tokens(db: Session, user: User, family_id: str, request: Request, now: datetime,
                  must_change_password: bool = False) -> TokenResponse:
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
        must_change_password=must_change_password,
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

    # only this (ip, username) pair is cleared; other addresses' failures stay on record
    db.execute(delete(LoginFailure).where(LoginFailure.username == key, _ip_clause(ip)))
    user.last_login_at = now
    tokens = _issue_tokens(db, user, family_id=uuid4().hex, request=request, now=now,
                           must_change_password=user_must_change_password(user))
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
        # 两个请求**真正同时**拿着同一个 refresh token 到达（输家走到这里）。
        # 典型场景是桌面窗口 + 浏览器两个页签同时刷新：旧逻辑把整族吊销，用户
        # 毫无征兆地被强制登出（现场反馈"登录已失效"的真实成因之一）。宽容窗口
        # 内的这种情况按良性并发处理：重读行确认消费时间，在窗口内就同族再签发
        # 一对新令牌，两个页签都活着。窗口之外的重放仍是攻击信号，维持整族吊销。
        # 重读消费时间。注意：本请求开头已经读过这行（当时 revoked_at 还是 NULL），
        # 身份映射里是旧快照——直接 scalar 会拿到过期值，赢家的提交根本看不见，
        # 宽容判断永远为 False。refresh() 强制从数据库重取。
        try:
            db.refresh(row)
        except Exception:  # pragma: no cover - 行被并发删除的极端情况
            pass
        if not _revoked_within_grace(row.revoked_at, now):
            revoke_family(db, row.family_id, now)
            record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_REUSE_DETECTED", entity="auth",
                         detail={"family": row.family_id, "reason": "concurrent"}, ip_address=ip)
            db.commit()
            raise invalid
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_CONCURRENT_GRACE", entity="auth",
                     detail={"family": row.family_id,
                             "age_s": round((now - _aware(row.revoked_at)).total_seconds(), 3)},
                     ip_address=ip)

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        revoke_family(db, row.family_id, now)
        record_audit(db, user_id=row.user_id, action="AUTH_REFRESH_DENIED_INACTIVE", entity="auth", ip_address=ip)
        db.commit()
        raise invalid

    tokens = _issue_tokens(db, user, family_id=row.family_id, request=request, now=now,
                           must_change_password=user_must_change_password(user))
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

    access.report_logout()
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
    user.must_change_password = False
    revoke_all_for_user(db, user.id, now)
    tokens = _issue_tokens(db, user, family_id=uuid4().hex, request=request, now=now,
                           must_change_password=user_must_change_password(user))
    record_audit(db, user_id=user.id, action="AUTH_PASSWORD_CHANGE", entity="users", entity_id=user.id, ip_address=ip)
    db.commit()
    return tokens


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = UserOut.model_validate(user)
    out.must_change_password = user_must_change_password(user)
    out.roles = access.role_codes_for(db, [user.id]).get(user.id, [])
    out.pages = access.allowed_pages(db, user)
    out.permission = access.permission_label(db, user)
    return out


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserSelfUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """本人改自己的资料：只允许姓名与邮箱。超管标志、启用状态、密码都不在这里改
    （密码走 /auth/change-password，状态与角色走系统设置里的账号管理）。"""
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(user, k, v)
    record_audit(db, user_id=user.id, action="PROFILE_UPDATE", entity="users", entity_id=user.id,
                 detail={"fields": sorted(data)},
                 ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    out = UserOut.model_validate(user)
    out.must_change_password = user_must_change_password(user)
    out.roles = access.role_codes_for(db, [user.id]).get(user.id, [])
    out.pages = access.allowed_pages(db, user)
    out.permission = access.permission_label(db, user)
    return out


@router.post("/ui-state", status_code=status.HTTP_204_NO_CONTENT)
def report_ui_state(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """界面上报“我现在登录着 / 已退出”，给托盘菜单显示登录状态。身份只取自令牌本身，上报不能冒名。"""
    payload = decode_access_token(creds.credentials) if (creds and creds.credentials) else None
    if user is not None and payload is not None:
        access.report_login(user, access.permission_label(db, user), float(payload["exp"]))
    else:
        access.report_logout()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ui-state")
def read_ui_state(
    alarms: bool = False,
    x_aims_launcher: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    """启动器（托盘）读取界面登录状态。需要启动器启动后端时注入的口令；没有口令（非托盘运行）时不开放。

    ``?alarms=1`` 时附带报警摘要（未处理数 / 严重数 / 最新几条 / 历史最大 id），托盘据此显示并在有新报警时弹气泡。
    摘要要查库，所以托盘不是每次都带，查不出来就给 null。
    """
    expected = os.environ.get("AIMS_LAUNCHER_TOKEN", "")
    if not expected or not x_aims_launcher or not hmac.compare_digest(expected, x_aims_launcher):
        raise HTTPException(status_code=404, detail="Not found")
    snap = access.snapshot()
    if alarms:
        try:
            snap["alarms"] = tray_summary.alarm_summary(db)
        except Exception:  # noqa: BLE001 - 库暂时不可用不能让托盘的登录信息也取不到
            snap["alarms"] = None
    return snap
