"""Password hashing, password policy and token helpers.

Hard rules (no fallbacks, in any environment):
  * Passwords are hashed with bcrypt ONLY. If `bcrypt` is missing the app fails
    to import instead of silently degrading to a weak scheme.
  * There is no SHA-256 (or any other) password fallback, and legacy
    "sha256$..." hashes are never accepted.
  * JWTs are always signed (HS256 via python-jose) and always verified. There is
    no unsigned/"demo." token format. Missing python-jose fails the import.
  * Access tokens are short lived; long sessions use rotating refresh tokens
    (opaque random strings, stored only as keyed digests in the database).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt as _bcrypt                      # required — ImportError is intentional
import jwt as pyjwt                           # required — ImportError is intentional
# 审计项 M-11：python-jose 已停止维护（CVE-2024-33663/33664 修复不完整，
# CVE-2026-85394 无修复版本），迁移到 PyJWT。本项目只签发/校验 HS256 对称
# 令牌，两个库 API 差异集中在 decode_access_token 一处。
from jwt import PyJWTError

from app.core.config import settings

_JWT_ALGORITHM = "HS256"
_RESERVED_CLAIMS = {"sub", "type", "iat", "nbf", "exp", "iss", "aud", "jti"}

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_BYTES = 72  # bcrypt input limit; we reject instead of silently truncating

_COMMON_PASSWORDS = {
    "password", "password1", "password123", "123456789012", "qwertyuiop12",
    "admin", "admin123", "administrator", "letmein", "welcome", "iloveyou",
    "changeme", "change-me", "aims", "aims123", "sys123", "op123", "maint123",
    "passw0rd", "p@ssw0rd", "1q2w3e4r5t6y", "abc123456789",
}


def validate_password_strength(password: str, username: str | None = None) -> None:
    """Raise ValueError with a human readable reason if `password` is unacceptable."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password is required")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_BYTES} bytes")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    low = password.lower()
    if low in _COMMON_PASSWORDS or re.sub(r"[\d\W_]+", "", low) in _COMMON_PASSWORDS:
        raise ValueError("Password is too common")
    if username and len(username) >= 3 and username.lower() in low:
        raise ValueError("Password must not contain the username")
    if len(set(password)) < 6:
        raise ValueError("Password is too repetitive")
    classes = sum(
        bool(re.search(p, password))
        for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3:
        raise ValueError("Password must mix at least 3 of: lowercase, uppercase, digits, symbols")


# ---------------------------------------------------------------------------
# Password hashing (bcrypt only)
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    data = plain.encode("utf-8")
    if len(data) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_BYTES} bytes")
    return _bcrypt.hashpw(data, _bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt check. Anything that is not a bcrypt hash is rejected
    (this includes legacy 'sha256$...' values — those accounts must be reset)."""
    if not plain or not hashed or not hashed.startswith("$2"):
        return False
    data = plain.encode("utf-8")
    if len(data) > PASSWORD_MAX_BYTES:
        return False
    try:
        return _bcrypt.checkpw(data, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH: str | None = None


def burn_password_check(plain: str) -> None:
    """Spend the same time as a real verification (used for unknown usernames so
    response timing does not reveal which accounts exist)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(plain, _DUMMY_HASH)


# ---------------------------------------------------------------------------
# Access tokens (signed JWT)
# ---------------------------------------------------------------------------
def create_access_token(subject: str | int, extra: Optional[dict[str, Any]] = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
    }
    for k, v in (extra or {}).items():
        if k not in _RESERVED_CLAIMS:
            payload[k] = v
    return pyjwt.encode(payload, settings.SECRET_KEY, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """Return verified claims of a valid *access* token, else None."""
    if not token or token.count(".") != 2:
        return None
    try:
        # PyJWT options 语义：require=[...] 与 jose 的 require_* 一一对应；
        # algorithms 显式钉死 HS256，杜绝 alg=none / RS-HS 混淆。
        payload = pyjwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[_JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={
                "require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"],
            },
        )
    except (PyJWTError, ValueError):
        return None
    if payload.get("type") != "access":
        return None
    return payload


# ---------------------------------------------------------------------------
# Refresh tokens (opaque, rotated, stored hashed)
# ---------------------------------------------------------------------------
def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Keyed digest (HMAC-SHA256) of a high-entropy random token. This is NOT a
    password hash: the input has 384 bits of entropy so a fast keyed digest is the
    correct primitive, and a database leak alone cannot be used to mint tokens."""
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
