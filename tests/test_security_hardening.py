"""Regression tests for the mandatory security controls:

SECRET_KEY injection, no default admin, no demo JWT, no SHA-256 fallback, CORS whitelist,
HTTPS, login throttling, token refresh, audit trail, upload safety.
"""
import importlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.core import config as config_mod
from app.core import security
from app.core.config import Settings, settings
from app.db import init_db as init_mod
from app.models.system import AuditLog, LoginFailure, RefreshToken, User

GOOD_PW = "Correct-Horse-Battery-9"
GOOD_KEY = "k" * 8 + "Xy7-" * 8  # 40 chars, not a placeholder


def _user(db, name="alice", pw=GOOD_PW, **kw):
    u = User(username=name, hashed_password=security.hash_password(pw), is_active=True, **kw)
    db.add(u)
    db.commit()
    return u


def _login(c, name="alice", pw=GOOD_PW):
    return c.post("/api/v1/auth/login", json={"username": name, "password": pw})


def _actions(db):
    return [r.action for r in db.scalars(select(AuditLog).order_by(AuditLog.id)).all()]


# ------------------------------------------------------------------ SECRET_KEY
def _mk(**kw):
    return Settings(_env_file=None, **kw)


_PROD = dict(APP_ENV="production", TRUSTED_PROXY_IPS="10.0.0.1")


@pytest.mark.parametrize("key", ["", "   ", "change-me", "change-me-to-a-random-secret", "short", "x" * 31])
def test_secret_key_missing_or_weak_is_rejected_in_production(key):
    with pytest.raises(ValidationError):
        _mk(SECRET_KEY=key, **_PROD)


def test_production_has_no_secret_default(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_PROD)


def test_dev_missing_secret_is_generated_once_and_persisted(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    a = _mk(DATABASE_URL="sqlite:///:memory:")
    assert len(a.SECRET_KEY) >= 32 and a.SECRET_KEY.lower() not in config_mod._WEAK_SECRETS
    saved = tmp_path / "aims_data" / ".dev_secret_key"
    assert saved.read_text().strip() == a.SECRET_KEY
    b = _mk(DATABASE_URL="sqlite:///:memory:")           # same key on the next start
    assert b.SECRET_KEY == a.SECRET_KEY


def test_dev_secret_lives_next_to_the_sqlite_file(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    db = tmp_path / "data" / "x.db"
    s = _mk(DATABASE_URL=f"sqlite:///{db}")
    assert (tmp_path / "data" / ".dev_secret_key").read_text().strip() == s.SECRET_KEY


def test_dev_weak_secret_is_accepted_with_warning(caplog):
    with caplog.at_level("WARNING"):
        s = _mk(SECRET_KEY="short")
    assert s.SECRET_KEY == "short"
    assert any("weak" in r.message.lower() for r in caplog.records)


def test_strong_secret_accepted():
    assert _mk(SECRET_KEY=GOOD_KEY).SECRET_KEY == GOOD_KEY
    assert _mk(SECRET_KEY=GOOD_KEY, **_PROD).SECRET_KEY == GOOD_KEY


# ------------------------------------------------------------------ dev admin switch
def test_dev_admin_allowed_only_outside_production():
    # Owner decision (after 1.0.0 phase 1 / H-2): the default admin/admin account is ON outside
    # production when ALLOW_DEV_ADMIN is unset, so a fresh desktop instance can be logged into.
    # Opt out with ALLOW_DEV_ADMIN=false; production ALWAYS refuses it.
    assert _mk(SECRET_KEY=GOOD_KEY).dev_admin_allowed is True
    assert _mk(SECRET_KEY=GOOD_KEY, ALLOW_DEV_ADMIN=True).dev_admin_allowed is True
    assert _mk(SECRET_KEY=GOOD_KEY, ALLOW_DEV_ADMIN=False).dev_admin_allowed is False
    assert _mk(SECRET_KEY=GOOD_KEY, **_PROD).dev_admin_allowed is False
    with pytest.raises(ValidationError):
        _mk(SECRET_KEY=GOOD_KEY, ALLOW_DEV_ADMIN=True, **_PROD)


# ------------------------------------------------------------------ CORS / HTTPS config
@pytest.mark.parametrize("origins", ["*", "null", "https://a.example,*", "ftp://x", "https://a.example/path"])
def test_cors_wildcards_and_junk_rejected(origins):
    with pytest.raises(ValidationError):
        _mk(SECRET_KEY=GOOD_KEY, CORS_ORIGINS=origins)


def test_cors_whitelist_parsed():
    s = _mk(SECRET_KEY=GOOD_KEY, CORS_ORIGINS="https://a.example, http://localhost:3000")
    assert s.cors_origin_list == ["https://a.example", "http://localhost:3000"]


def test_production_requires_https_setup_and_https_origins():
    base = dict(SECRET_KEY=GOOD_KEY, APP_ENV="production")
    with pytest.raises(ValidationError):
        _mk(**base)  # no TLS cert and no trusted proxy
    with pytest.raises(ValidationError):
        _mk(**base, TRUSTED_PROXY_IPS="10.0.0.1", CORS_ORIGINS="http://ui.example")
    with pytest.raises(ValidationError):
        _mk(**base, TRUSTED_PROXY_IPS="10.0.0.1", REQUIRE_HTTPS=False)
    with pytest.raises(ValidationError):
        _mk(**base, TRUSTED_PROXY_IPS="10.0.0.1", SEED_DEMO_DATA=True)
    ok = _mk(**base, TRUSTED_PROXY_IPS="10.0.0.1", CORS_ORIGINS="https://ui.example")
    assert ok.https_required and not ok.docs_enabled
    ok2 = _mk(**base, SSL_CERTFILE="c.pem", SSL_KEYFILE="k.pem")
    assert ok2.tls_direct


def test_access_token_lifetime_is_bounded():
    with pytest.raises(ValidationError):
        _mk(SECRET_KEY=GOOD_KEY, ACCESS_TOKEN_EXPIRE_MINUTES=1440)


def test_https_middleware_blocks_plain_http(monkeypatch, db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.middleware import HTTPSEnforcementMiddleware

    mini = FastAPI()
    mini.add_middleware(HTTPSEnforcementMiddleware, exempt_paths=("/health",))

    @mini.get("/x")
    def x():
        return {"ok": True}

    @mini.get("/health")
    def h():
        return {"ok": True}

    assert TestClient(mini, base_url="http://t").get("/x").status_code == 403
    assert TestClient(mini, base_url="https://t").get("/x").status_code == 200
    assert TestClient(mini, base_url="http://t").get("/health").status_code == 200


def test_cors_only_allows_whitelisted_origin(anon_client):
    ok = anon_client.options("/api/v1/devices", headers={
        "Origin": "https://ui.example.test", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "https://ui.example.test"
    bad = anon_client.options("/api/v1/devices", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
    null = anon_client.options("/api/v1/devices", headers={
        "Origin": "null", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in null.headers


def test_security_headers_present(anon_client):
    r = anon_client.get("/api/v1/devices")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ passwords / JWT
def test_no_sha256_fallback_and_legacy_hashes_rejected():
    import hashlib

    legacy = "sha256$" + hashlib.sha256(b"whatever-pw-123").hexdigest()
    assert security.verify_password("whatever-pw-123", legacy) is False
    assert security.hash_password("Some-Pass-1234!").startswith("$2")
    src = open(security.__file__, encoding="utf-8").read()
    assert "hashlib.sha256(plain" not in src and '"sha256$"' not in src


def test_security_module_has_no_optional_import_fallbacks():
    src = open(security.__file__, encoding="utf-8").read()
    # 阶段 1（审计项 M-11）：python-jose → PyJWT，导入失败必须直接报错而非静默降级
    assert "import bcrypt as _bcrypt" in src and "import jwt as pyjwt" in src
    assert "from jose import" not in src
    assert "try:\n    import bcrypt" not in src and "_jose()" not in src
    assert 'startswith("demo.")' not in src and '"demo." +' not in src


def test_password_policy():
    for bad in ("admin123", "short1A!", "aaaaaaaaaaaaaaaa", "lowercaseonlylongpassword", "Password123456"):
        with pytest.raises(ValueError):
            security.validate_password_strength(bad)
    with pytest.raises(ValueError):
        security.validate_password_strength("alice-Password-9999", "alice")
    security.validate_password_strength(GOOD_PW)


def test_demo_and_forged_tokens_rejected(anon_client, db_session):
    import base64, json

    u = _user(db_session)
    demo = "demo." + base64.urlsafe_b64encode(json.dumps({"sub": str(u.id)}).encode()).decode()
    assert security.decode_access_token(demo) is None
    r = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {demo}"})
    assert r.status_code == 401

    # alg=none
    hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    pl = base64.urlsafe_b64encode(json.dumps({"sub": str(u.id), "type": "access"}).encode()).rstrip(b"=").decode()
    assert security.decode_access_token(f"{hdr}.{pl}.") is None

    # signed with another key
    import jwt

    forged = jwt.encode({"sub": str(u.id), "type": "access"}, "z" * 40, algorithm="HS256")
    assert security.decode_access_token(forged) is None


def test_refresh_token_cannot_be_used_as_access_token(anon_client, db_session):
    _user(db_session)
    body = _login(anon_client).json()
    assert security.decode_access_token(body["refresh_token"]) is None
    r = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['refresh_token']}"})
    assert r.status_code == 401


def test_access_token_claims(anon_client, db_session):
    _user(db_session)
    tok = _login(anon_client).json()["access_token"]
    claims = security.decode_access_token(tok)
    assert claims["type"] == "access" and claims["iss"] == settings.JWT_ISSUER
    assert claims["aud"] == settings.JWT_AUDIENCE and claims["jti"]
    assert claims["exp"] - claims["iat"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_expired_access_token_rejected(db_session):
    import jwt

    u = _user(db_session)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    tok = jwt.encode({"sub": str(u.id), "type": "access", "iat": past - timedelta(minutes=10), "nbf": past - timedelta(minutes=10),
                      "exp": past, "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE, "jti": "x"},
                     settings.SECRET_KEY, algorithm="HS256")
    assert security.decode_access_token(tok) is None


# ------------------------------------------------------------------ no default admin
def test_init_db_creates_no_users_when_dev_admin_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    assert db_session.scalar(select(User)) is None
    for name, pw in (("admin", "admin123"), ("system", "sys123"), ("operator", "op123"), ("maintainer", "maint123")):
        assert _login_direct(db_session, name, pw) is False


def _login_direct(db, name, pw):
    u = db.scalar(select(User).where(User.username == name))
    return bool(u and security.verify_password(pw, u.hashed_password))


def test_bootstrap_admin_from_injected_credentials(db_session, monkeypatch):
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "root-ops")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", GOOD_PW)
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    u = db_session.scalar(select(User).where(User.username == "root-ops"))
    assert u and u.is_superuser and security.verify_password(GOOD_PW, u.hashed_password)
    init_mod._bootstrap_admin(db_session)  # idempotent: table no longer empty
    assert len(db_session.scalars(select(User)).all()) == 1


def test_bootstrap_rejects_weak_password(db_session, monkeypatch):
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "root-ops")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "admin123")
    with pytest.raises(RuntimeError):
        init_mod._bootstrap_admin(db_session)


def test_legacy_demo_accounts_in_old_databases_are_disabled(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    db_session.add(User(username="admin", hashed_password=security.hash_password("admin123"), is_superuser=True, is_active=True))
    db_session.add(User(username="operator", hashed_password=security.hash_password("My-own-Strong-Pw-1"), is_active=True))
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 1
    assert db_session.scalar(select(User).where(User.username == "admin")).is_active is False
    assert db_session.scalar(select(User).where(User.username == "operator")).is_active is True  # custom password untouched
    assert "SECURITY_DEFAULT_ACCOUNT_DISABLED" in _actions(db_session)


def test_dev_admin_created_on_empty_db_and_can_log_in(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", True)
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    u = db_session.scalar(select(User).where(User.username == "admin"))
    assert u and u.is_superuser and u.is_active
    assert security.verify_password("admin123", u.hashed_password)     # 默认 admin / admin123
    assert not security.verify_password("admin", u.hashed_password)
    init_mod._bootstrap_admin(db_session)                 # idempotent
    assert len(db_session.scalars(select(User)).all()) == 1


def test_injected_bootstrap_credentials_win_over_dev_admin(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", True)
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "root-ops")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", GOOD_PW)
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    names = [u.username for u in db_session.scalars(select(User)).all()]
    assert names == ["root-ops"]


def test_dev_admin_survives_startup_check_but_other_demo_accounts_do_not(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", True)
    db_session.add(User(username="admin", hashed_password=security.hash_password("admin"), is_superuser=True, is_active=True))
    db_session.add(User(username="operator", hashed_password=security.hash_password("op123"), is_active=True))
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 1
    assert db_session.scalar(select(User).where(User.username == "admin")).is_active is True
    assert db_session.scalar(select(User).where(User.username == "operator")).is_active is False


def test_default_admin_admin_is_disabled_when_dev_admin_not_allowed(db_session, monkeypatch):
    """切到生产环境 / ALLOW_DEV_ADMIN=false 之后，库里遗留的 admin/admin 必须在启动时被停用。"""
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    db_session.add(User(username="admin", hashed_password=security.hash_password("admin"), is_superuser=True, is_active=True))
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 1
    assert db_session.scalar(select(User).where(User.username == "admin")).is_active is False


def test_admin_admin123_and_old_admin_admin_survive_when_dev_admin_allowed(db_session, monkeypatch):
    """默认口令是 admin123；开发管理员放行时，admin/admin123 和旧版 admin/admin 的老库账号升级后都不能被停用。"""
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", True)
    db_session.add(User(username="admin", hashed_password=security.hash_password("admin123"), is_superuser=True, is_active=True))
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 0
    u = db_session.scalar(select(User).where(User.username == "admin"))
    u.hashed_password = security.hash_password("admin")
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 0
    assert u.is_active is True


def test_admin123_account_is_disabled_when_dev_admin_not_allowed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    db_session.add(User(username="admin", hashed_password=security.hash_password("admin123"), is_superuser=True, is_active=True))
    db_session.commit()
    assert init_mod._disable_legacy_demo_accounts(db_session) == 1


def test_dev_default_credential_helper(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", True)
    assert security.is_dev_default_credential("admin", "admin123") is True
    assert security.is_dev_default_credential("admin", "admin") is False
    assert security.is_dev_default_credential("someone", "admin123") is False
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    assert security.is_dev_default_credential("admin", "admin123") is False


def test_api_still_rejects_weak_passwords_even_for_the_default_pair(client):
    """admin/admin 只对"首次创建 / CLI"放行，HTTP 接口的密码策略不变。"""
    r = client.post("/api/v1/system/users", json={"username": "admin2", "password": "admin"})
    assert r.status_code == 422


def test_dev_admin_never_used_when_disallowed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", False)
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    assert db_session.scalar(select(User)) is None


def test_root_endpoint_does_not_leak_credentials(anon_client):
    text = anon_client.get("/").text.lower()
    assert "admin123" not in text and "password" not in text


def test_frontend_has_no_demo_credentials():
    from app.main import FRONTEND_PATH

    if FRONTEND_PATH:
        html = FRONTEND_PATH.read_text(encoding="utf-8")
        for needle in ("op123", "maint123", "sys123"):
            assert needle not in html
        # 默认账号 admin / admin123 按产品要求写在登录页提示里；除此之外前端不应出现这个口令
        assert html.count("admin123") == html.count("admin / admin123")


def test_user_creation_enforces_password_policy(client):
    r = client.post("/api/v1/system/users", json={"username": "newbie", "password": "admin123"})
    assert r.status_code == 422
    r = client.post("/api/v1/system/users", json={"username": "newbie", "password": GOOD_PW})
    assert r.status_code == 201


# ------------------------------------------------------------------ login throttling
def test_lockout_after_repeated_failures_and_correct_password_does_not_help(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    _user(db_session)
    for _ in range(3):
        assert _login(anon_client, pw="wrong-password-1").status_code == 401
    r = _login(anon_client)  # correct password, but locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    acts = _actions(db_session)
    assert acts.count("AUTH_LOGIN_FAILED") == 3 and "AUTH_LOGIN_BLOCKED" in acts


def test_lockout_expires(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 2)
    _user(db_session)
    for _ in range(2):
        _login(anon_client, pw="wrong-password-1")
    assert _login(anon_client).status_code == 429
    old = datetime.now(timezone.utc) - timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES + 1)
    for row in db_session.scalars(select(LoginFailure)).all():
        row.created_at = old
    db_session.commit()
    assert _login(anon_client).status_code == 200


def test_unknown_user_is_throttled_like_a_real_one(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 2)
    for _ in range(2):
        assert _login(anon_client, "ghost", "whatever-pw-1").status_code == 401
    assert _login(anon_client, "ghost", "whatever-pw-1").status_code == 429


def test_per_ip_limit_blocks_username_spraying(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 3)
    for i in range(3):
        assert _login(anon_client, f"user{i}", "whatever-pw-1").status_code == 401
    assert _login(anon_client, "another", "whatever-pw-1").status_code == 429


def test_success_clears_failure_counter(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    _user(db_session)
    for _ in range(2):
        _login(anon_client, pw="wrong-password-1")
    assert _login(anon_client).status_code == 200
    assert db_session.scalars(select(LoginFailure)).all() == []
    assert _login(anon_client, pw="wrong-password-1").status_code == 401  # not locked


def test_login_error_is_generic(anon_client, db_session):
    _user(db_session)
    a = _login(anon_client, "alice", "wrong-password-1")
    b = _login(anon_client, "nobody", "wrong-password-1")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


# ------------------------------------------------------------------ refresh tokens
def test_refresh_rotates_and_old_token_is_dead(anon_client, db_session):
    _user(db_session)
    t1 = _login(anon_client).json()
    r = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    assert anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {t2['access_token']}"}).status_code == 200


def test_refresh_reuse_revokes_whole_family(anon_client, db_session):
    _user(db_session)
    t1 = _login(anon_client).json()
    t2 = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    replay = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert replay.status_code == 401
    # the legitimate newest token is now dead too
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert "AUTH_REFRESH_REUSE_DETECTED" in _actions(db_session)


def test_refresh_rejects_garbage_expired_and_disabled(anon_client, db_session):
    u = _user(db_session)
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 40}).status_code == 401
    t = _login(anon_client).json()
    row = db_session.scalar(select(RefreshToken))
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    t = _login(anon_client).json()
    u.is_active = False
    db_session.commit()
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_refresh_tokens_are_stored_hashed(anon_client, db_session):
    _user(db_session)
    t = _login(anon_client).json()
    row = db_session.scalar(select(RefreshToken))
    assert row.token_hash != t["refresh_token"] and t["refresh_token"] not in row.token_hash


def test_logout_revokes_session(anon_client, db_session):
    _user(db_session)
    t = _login(anon_client).json()
    r = anon_client.post("/api/v1/auth/logout", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 204
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert "AUTH_LOGOUT" in _actions(db_session)


def test_change_password_revokes_other_sessions(anon_client, db_session):
    _user(db_session)
    t = _login(anon_client).json()
    h = {"Authorization": f"Bearer {t['access_token']}"}
    assert anon_client.post("/api/v1/auth/change-password", headers=h,
                            json={"current_password": "wrong-password-1", "new_password": "Brand-New-Pass-77!"}).status_code == 400
    assert anon_client.post("/api/v1/auth/change-password", headers=h,
                            json={"current_password": GOOD_PW, "new_password": "admin123"}).status_code == 422
    r = anon_client.post("/api/v1/auth/change-password", headers=h,
                         json={"current_password": GOOD_PW, "new_password": "Brand-New-Pass-77!"})
    assert r.status_code == 200
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert _login(anon_client, pw="Brand-New-Pass-77!").status_code == 200
    assert _login(anon_client).status_code == 401


# ------------------------------------------------------------------ audit trail
def test_login_events_audited_without_secrets(anon_client, db_session):
    _user(db_session)
    _login(anon_client, pw="wrong-password-1")
    _login(anon_client)
    acts = _actions(db_session)
    assert "AUTH_LOGIN_FAILED" in acts and "AUTH_LOGIN_SUCCESS" in acts
    blob = " ".join(str(r.detail) for r in db_session.scalars(select(AuditLog)).all())
    assert "wrong-password-1" not in blob and GOOD_PW not in blob


def test_every_mutating_call_is_audited_with_actor(anon_client, db_session):
    u = _user(db_session)
    tok = _login(anon_client).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = anon_client.post("/api/v1/devices", headers=h, json={"device_code": "AUD-1", "name": "x", "protocol": "Modbus TCP"})
    assert r.status_code == 201
    rows = [a for a in db_session.scalars(select(AuditLog)).all() if a.action == "API_POST" and a.entity == "/api/v1/devices"]
    assert rows and rows[-1].user_id == u.id and rows[-1].detail["status"] == 201 and rows[-1].ip_address


def test_denied_and_anonymous_writes_are_audited(anon_client, db_session):
    anon_client.post("/api/v1/devices", json={"device_code": "X", "name": "x", "protocol": "Modbus TCP"})
    row = [a for a in db_session.scalars(select(AuditLog)).all() if a.action == "API_POST"][-1]
    assert row.detail["outcome"] == "denied" and row.user_id is None


def test_reads_are_not_written_to_audit(anon_client, db_session):
    anon_client.get("/api/v1/devices")
    assert "API_GET" not in _actions(db_session)


def test_user_admin_actions_audited_and_deactivation_ends_sessions(anon_client, db_session):
    admin = _user(db_session, "boss", is_superuser=True)
    victim = _user(db_session, "victim")
    vt = _login(anon_client, "victim").json()
    at = _login(anon_client, "boss").json()["access_token"]
    r = anon_client.patch(f"/api/v1/system/users/{victim.id}", headers={"Authorization": f"Bearer {at}"}, json={"is_active": False})
    assert r.status_code == 200
    assert "USER_UPDATE" in _actions(db_session)
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": vt["refresh_token"]}).status_code == 401
    assert admin.id


# ------------------------------------------------------------------ uploads
def _xlsx_with(extra: dict):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["device_code", "name"])
    wb.active.append(["UP-1", "n"])
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i in src.infolist():
            z.writestr(i.filename, src.read(i.filename))
        for n, d in extra.items():
            z.writestr(n, d)
    return out.getvalue()


def _post(client, name, data, path="/api/v1/devices/import"):
    return client.post(path, files={"file": (name, io.BytesIO(data), "application/octet-stream")})


def test_upload_valid_csv_and_xlsx_still_work(client):
    assert _post(client, "d.csv", b"device_code,name\nUP-CSV,n\n").status_code == 201
    assert _post(client, "d.xlsx", _xlsx_with({})).status_code == 201


@pytest.mark.parametrize("name,data", [
    ("evil.exe", b"MZ\x90\x00"),
    ("evil.php", b"<?php ?>"),
    ("noext", b"device_code,name\n"),
    ("d.csv.exe", b"device_code,name\n"),
])
def test_upload_extension_whitelist(client, name, data):
    assert _post(client, name, data).status_code == 400


def test_upload_content_must_match_extension(client):
    assert _post(client, "fake.csv", _xlsx_with({})).status_code == 400          # xlsx renamed .csv
    assert _post(client, "fake.csv", b"MZ\x90\x00\x03,a\n").status_code == 400    # PE renamed .csv
    assert _post(client, "fake.csv", b"a,b\n\x00\x01\x02\n").status_code == 400   # NUL bytes
    assert _post(client, "fake.xlsx", b"device_code,name\nA,b\n").status_code == 400
    assert _post(client, "fake.xls", b"device_code,name\nA,b\n").status_code == 400


def test_upload_rejects_macros_embedded_objects_and_zip_bombs(client):
    assert _post(client, "m.xlsx", _xlsx_with({"xl/vbaProject.bin": b"x"})).status_code == 400
    assert _post(client, "o.xlsx", _xlsx_with({"xl/embeddings/oleObject1.bin": b"x"})).status_code == 400
    assert _post(client, "l.xlsx", _xlsx_with({"xl/externalLinks/externalLink1.xml": b"x"})).status_code == 400
    assert _post(client, "b.xlsx", _xlsx_with({"xl/worksheets/bomb.xml": b"0" * (60 * 1024 * 1024)})).status_code in (400, 413)


def test_upload_size_limit_413(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    big = b"device_code,name\n" + b"A,b\n" * 2000
    assert _post(client, "big.csv", big).status_code == 413


def test_upload_row_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_ROWS", 5)
    data = b"device_code,name\n" + b"".join(b"R%d,n\n" % i for i in range(20))
    assert _post(client, "rows.csv", data).status_code in (400, 413)


def test_all_upload_endpoints_use_the_guard(client):
    paths = ["/api/v1/devices/import", "/api/v1/devices/tags/import", "/api/v1/gateways/import",
             "/api/v1/assets/import", "/api/v1/lab/samples/import", "/api/v1/knowledge/documents/import",
             "/api/v1/maintenance/work-orders/import", "/api/v1/import-validation/preview"]
    for p in paths:
        assert _post(client, "evil.exe", b"MZ", p).status_code == 400, p
        assert _post(client, "fake.csv", b"MZ\x90\x00", p).status_code == 400, p
    for p in ("/api/v1/measurements/import", "/api/v1/analysis/import"):
        r = client.post(p, data={"device_id": 1}, files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
        assert r.status_code in (400, 404), p


def test_upload_is_audited_with_hash_and_sanitised_name(anon_client, db_session):
    _user(db_session)
    tok = _login(anon_client).json()["access_token"]
    r = anon_client.post("/api/v1/devices/import", headers={"Authorization": f"Bearer {tok}"},
                         files={"file": ("../../etc/<b>x</b>.csv", io.BytesIO(b"device_code,name\nAUD-UP,n\n"), "text/csv")})
    assert r.status_code == 201
    row = [a for a in db_session.scalars(select(AuditLog)).all() if a.detail.get("upload")][-1]
    up = row.detail["upload"]
    assert len(up["sha256"]) == 64 and "/" not in up["filename"] and "<" not in up["filename"]


def test_rejected_upload_is_audited(anon_client, db_session):
    _user(db_session)
    tok = _login(anon_client).json()["access_token"]
    anon_client.post("/api/v1/devices/import", headers={"Authorization": f"Bearer {tok}"},
                     files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    row = [a for a in db_session.scalars(select(AuditLog)).all() if a.action == "API_POST"][-1]
    assert row.detail["status"] == 400 and "upload_rejected" in row.detail


def test_request_body_cap_rejects_before_parsing(anon_client, monkeypatch):
    from app.core.middleware import RequestSizeLimitMiddleware  # noqa: F401  (wired in main)

    big = b"a" * (settings.MAX_UPLOAD_BYTES + 2 * 1024 * 1024)
    r = anon_client.post("/api/v1/devices/import", files={"file": ("x.csv", io.BytesIO(big), "text/csv")})
    assert r.status_code == 413


# ------------------------------------------------------------------ frontend behaviour (static)
def _frontend_html():
    from app.main import FRONTEND_PATH

    assert FRONTEND_PATH, "frontend.html not found"
    return FRONTEND_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", [
    "aimsPrecheckFile",              # pre-upload checks
    "AIMS_MAX_UPLOAD_BYTES",
    "aimsStartLockout",              # 429 countdown
    "retryAfter",
    "aimsScheduleRefresh",           # proactive refresh
    "aimsSessionExpired",            # expiry handling
    "sessionToast",
    "addEventListener('storage'",    # cross-tab sync
])
def test_frontend_has_session_and_upload_guards(needle):
    assert needle in _frontend_html()


def test_frontend_precheck_runs_before_every_table_upload():
    html = _frontend_html()
    body = html[html.index("function handleTableImport"):]
    body = body[:body.index("// ===== Translations")]
    assert body.index("aimsPrecheckFile") < body.index("aimsUpload(")



# ------------------------------------------------------------------ 并发刷新宽容
# 真实场景：桌面窗口 + 浏览器两个页签同时拿着同一份 refresh token 刷新。旧逻辑
# 把后到的请求判成"重放"并吊销整族令牌——用户毫无征兆被强制登出（现场反馈
# "登录已失效"的真实成因之一）。宽容窗口内的真并发应同族再签发；窗口外的
# 顺序重放仍然是攻击信号，维持整族吊销。
def _racing_db(db_session, row, monkeypatch):
    """让 db_session 模拟"竞态输家"：读行时令牌未消费，条件 UPDATE 之前赢家先落库。

    直接把行改成已撤销是不行的——那样输家的读行就会走进顺序重放分支，测不到
    rowcount == 0 的真并发路径。这里拦截对 refresh_tokens 的条件 UPDATE，
    先替"赢家"落库，再执行输家原本的语句（自然匹配 0 行）。
    """
    from sqlalchemy import update as sa_update

    applied = {"done": False}
    original = db_session.execute

    def racing_execute(statement, *args, **kwargs):
        if (not applied["done"] and getattr(statement, "is_update", False)
                and statement.table.name == "refresh_tokens"):
            applied["done"] = True
            original(sa_update(RefreshToken)
                     .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
                     .values(revoked_at=datetime.now(timezone.utc)))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", racing_execute)


def test_concurrent_refresh_race_within_grace_keeps_family_alive(anon_client, db_session, monkeypatch):
    from types import SimpleNamespace

    from app.api.v1.auth import refresh
    from app.schemas.auth import RefreshRequest

    _user(db_session)
    t1 = _login(anon_client).json()
    row = db_session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(t1["refresh_token"])))

    _racing_db(db_session, row, monkeypatch)

    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                          headers={"user-agent": "grace-test"})
    resp = refresh(RefreshRequest(refresh_token=t1["refresh_token"]), request=req, db=db_session)
    assert resp.access_token, "竞态输家必须拿到新令牌，而不是被登出"
    # 新 access token 真的可用（同族签发、用户有效）
    r = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {resp.access_token}"})
    assert r.status_code == 200


def test_concurrent_refresh_grace_can_be_disabled(anon_client, db_session, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.v1.auth import refresh
    from app.schemas.auth import RefreshRequest

    monkeypatch.setenv("AIMS_REFRESH_GRACE_SECONDS", "0")
    _user(db_session)
    t1 = _login(anon_client).json()
    row = db_session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(t1["refresh_token"])))
    _racing_db(db_session, row, monkeypatch)

    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                          headers={"user-agent": "grace-test"})
    with pytest.raises(HTTPException) as ei:
        refresh(RefreshRequest(refresh_token=t1["refresh_token"]), request=req, db=db_session)
    assert ei.value.status_code == 401


def test_sequential_replay_still_revokes_family(anon_client, db_session):
    """宽容窗口只覆盖"真并发"（条件 UPDATE 竞态）；事后重放维持整族吊销。"""
    _user(db_session)
    t1 = _login(anon_client).json()
    t2 = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    assert t2["access_token"]
    # 顺序重放走 revoked_at 分支（非竞态），必须 401 且新令牌也死
    replay = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert replay.status_code == 401
    assert anon_client.post("/api/v1/auth/refresh",
                            json={"refresh_token": t2["refresh_token"]}).status_code == 401
