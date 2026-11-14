"""首次登录默认管理员口令必须先改密码。

登录 / 刷新 / /auth/me 在口令仍是内置默认口令（admin / admin123，未定制安装口令时）
时返回 must_change_password=true，前端据此把用户送去修改密码，其余功能全部
挡住。改完密码后标志自动消失（新口令按强度策略不可能再是默认口令）。

服务端也强制：被标记的账号调 /auth/me、改密码、登出、刷新以外的任何 /api
接口都返回 403 password_change_required，不只靠前端拦。标志有两个来源：
建账号时打上的列（_bootstrap_admin，受 DEV_ADMIN_FORCE_PASSWORD_CHANGE 开关
控制），以及“当前存着的口令哈希是不是默认口令”这一现算检查（覆盖老库升级）。

默认口令本身不动（admin/admin123 仍是全新安装的默认账号），已有数据库的口令也
不动：标志只认“当前存着的口令哈希是不是默认口令”或建账号时打上的标志。
"""
from __future__ import annotations

from sqlalchemy import create_engine, select, text

from app.core import security
from app.core.config import settings
from app.db import init_db as init_mod
from app.models.system import User

DEFAULT_PW = "admin123"
NEW_PW = "New-Strong-Pw-42"
OTHER_PW = "Another-Good-Pw-7"


def _make_dev_admin(db, monkeypatch, *, force=True):
    """用 _bootstrap_admin 建一个开发管理员（带存着的标志），与全新安装一致。"""
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", None)
    monkeypatch.setattr(settings, "DEV_ADMIN_FORCE_PASSWORD_CHANGE", force)
    init_mod._seed_roles(db)
    init_mod._bootstrap_admin(db)


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def _make_user(db, username, password, **kw):
    user = User(username=username, hashed_password=security.hash_password(password),
                is_active=True, **kw)
    db.add(user)
    db.commit()
    return user


def _login(c, name, pw):
    return c.post("/api/v1/auth/login", json={"username": name, "password": pw})


def _auth_header(c, name, pw):
    r = _login(c, name, pw)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_default_admin_login_reports_must_change_password(anon_client, db_session):
    _make_user(db_session, "admin", DEFAULT_PW, is_superuser=True)
    r = _login(anon_client, "admin", DEFAULT_PW)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True


def test_regular_user_login_does_not_flag(anon_client, db_session):
    _make_user(db_session, "alice", NEW_PW)
    r = _login(anon_client, "alice", NEW_PW)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False


def test_customized_admin_password_does_not_flag(anon_client, db_session):
    """管理员账号本身不触发标志，触发的是“口令仍是默认口令”。"""
    _make_user(db_session, "admin", NEW_PW, is_superuser=True)
    r = _login(anon_client, "admin", NEW_PW)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False


def test_me_and_refresh_report_the_flag(anon_client, db_session):
    _make_user(db_session, "admin", DEFAULT_PW, is_superuser=True)
    r = _login(anon_client, "admin", DEFAULT_PW)
    body = r.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = anon_client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    refreshed = anon_client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["must_change_password"] is True


def test_change_password_clears_the_flag(anon_client, db_session):
    _make_user(db_session, "admin", DEFAULT_PW, is_superuser=True)
    headers = _auth_header(anon_client, "admin", DEFAULT_PW)
    r = anon_client.post("/api/v1/auth/change-password", headers=headers,
                         json={"current_password": DEFAULT_PW, "new_password": NEW_PW})
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is False

    # 新口令登录不再提示；/auth/me 也不再提示。
    r2 = _login(anon_client, "admin", NEW_PW)
    assert r2.status_code == 200
    assert r2.json()["must_change_password"] is False
    me = anon_client.get("/api/v1/auth/me",
                         headers={"Authorization": f"Bearer {r2.json()['access_token']}"})
    assert me.json()["must_change_password"] is False


def test_production_never_flags(anon_client, db_session, monkeypatch):
    """生产环境下默认账号本就不被放行，标志恒为 False。"""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    _make_user(db_session, "admin", DEFAULT_PW, is_superuser=True)
    r = _login(anon_client, "admin", DEFAULT_PW)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False


def test_flagged_user_blocked_except_allowed(anon_client, db_session, monkeypatch):
    """服务端强制：被标记的账号调其他 /api 接口一律 403，只放行 me、改密码、登出、刷新。"""
    _make_dev_admin(db_session, monkeypatch)
    body = _login(anon_client, "admin", DEFAULT_PW).json()
    tok, rt = body["access_token"], body["refresh_token"]
    for path in ("/api/v1/devices", "/api/v1/system/settings", "/api/v1/system/users"):
        r = anon_client.get(path, headers=_hdr(tok))
        assert r.status_code == 403 and r.json()["detail"] == "password_change_required", path
    me = anon_client.get("/api/v1/auth/me", headers=_hdr(tok))
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    refreshed = anon_client.post("/api/v1/auth/refresh", json={"refresh_token": rt})
    assert refreshed.status_code == 200 and refreshed.json()["must_change_password"] is True


def test_changing_password_lifts_the_server_block(anon_client, db_session, monkeypatch):
    _make_dev_admin(db_session, monkeypatch)
    body = _login(anon_client, "admin", DEFAULT_PW).json()
    tok = body["access_token"]
    assert anon_client.get("/api/v1/devices", headers=_hdr(tok)).status_code == 403
    r = anon_client.post("/api/v1/auth/change-password", headers=_hdr(tok),
                         json={"current_password": DEFAULT_PW, "new_password": NEW_PW})
    assert r.status_code == 200 and r.json()["must_change_password"] is False
    new = r.json()["access_token"]
    assert anon_client.get("/api/v1/devices", headers=_hdr(new)).status_code == 200
    assert anon_client.get("/api/v1/auth/me", headers=_hdr(new)).json()["must_change_password"] is False
    assert _login(anon_client, "admin", DEFAULT_PW).status_code == 401      # admin/admin123 作废
    again = _login(anon_client, "admin", NEW_PW)
    assert again.status_code == 200 and again.json()["must_change_password"] is False
    # 登出接口在被标记时也是放行的。
    out = anon_client.post("/api/v1/auth/logout", headers=_hdr(new),
                           json={"refresh_token": again.json()["refresh_token"]})
    assert out.status_code == 204


def test_switch_off_means_no_forced_change(anon_client, db_session, monkeypatch):
    _make_dev_admin(db_session, monkeypatch, force=False)
    body = _login(anon_client, "admin", DEFAULT_PW).json()
    assert body["must_change_password"] is False
    assert anon_client.get("/api/v1/devices", headers=_hdr(body["access_token"])).status_code == 200


def test_custom_bootstrap_password_is_not_flagged(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "ops")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "Rea1-Strong-Pw-42")
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    assert db_session.scalar(select(User).where(User.username == "ops")).must_change_password is False
    r = _login(anon_client, "ops", "Rea1-Strong-Pw-42")
    assert r.status_code == 200 and r.json()["must_change_password"] is False


def test_ordinary_authenticated_client_is_unaffected(client):
    assert client.get("/api/v1/devices").status_code == 200


def test_old_sqlite_users_table_gets_the_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), hashed_password VARCHAR(255))"))
        c.execute(text("INSERT INTO users (username, hashed_password) VALUES ('legacy', 'x')"))
    init_mod._ensure_sqlite_chain_columns(eng)
    with eng.begin() as c:
        assert c.execute(text("SELECT must_change_password FROM users")).scalar() == 0
