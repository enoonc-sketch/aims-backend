"""设置页账号中心（1.0.10）：删账号、建/删角色、代建账号与他人重置密码后首次登录强制改密、本人改资料。"""
from __future__ import annotations

import pytest

from app.core import security
from app.db import init_db as init_mod
from app.models.system import Role, User

GOOD = "Correct-Horse-Battery-9"
NEW = "Another-Strong-Pw-77"
CREATED_PW = "Created-Account-Pw-8"


@pytest.fixture()
def seeded(db_session):
    init_mod._seed_roles(db_session)
    init_mod._seed_permissions(db_session)
    db_session.commit()
    return db_session


def _mk(db, name, superuser=False):
    u = User(username=name, hashed_password=security.hash_password(GOOD),
             is_active=True, is_superuser=superuser)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _login(c, name, pw):
    r = c.post("/api/v1/auth/login", json={"username": name, "password": pw})
    return r, ({"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else {})


def _hdr(c, name, pw=GOOD):
    r, h = _login(c, name, pw)
    assert r.status_code == 200, r.text
    return h


# ------------------------------------------------------------------ 删账号
def test_delete_user_removes_account_and_sessions(anon_client, db_session):
    _mk(db_session, "boss", True)
    worker = _mk(db_session, "worker")
    _login(anon_client, "worker", GOOD)  # 先登录一次，留下一个会话/刷新令牌
    rt = anon_client.post("/api/v1/auth/login",
                          json={"username": "worker", "password": GOOD}).json()["refresh_token"]
    hb = _hdr(anon_client, "boss")
    r = anon_client.delete(f"/api/v1/system/users/{worker.id}", headers=hb)
    assert r.status_code == 204
    assert db_session.get(User, worker.id) is None
    assert _login(anon_client, "worker", GOOD)[0].status_code == 401
    assert anon_client.post("/api/v1/auth/refresh",
                            json={"refresh_token": rt}).status_code == 401  # 旧会话被踢


def test_delete_self_and_last_superuser_are_refused(anon_client, db_session):
    boss = _mk(db_session, "boss", True)
    hb = _hdr(anon_client, "boss")
    assert anon_client.delete(f"/api/v1/system/users/{boss.id}", headers=hb).status_code == 400
    assert db_session.get(User, boss.id) is not None


def test_non_superuser_cannot_delete_users(anon_client, db_session):
    _mk(db_session, "boss", True)
    victim = _mk(db_session, "victim")
    hw = _hdr(anon_client, "victim")
    assert anon_client.delete(f"/api/v1/system/users/{victim.id}", headers=hw).status_code == 403
    assert db_session.get(User, victim.id) is not None


def test_delete_missing_user_is_404(anon_client, db_session):
    _mk(db_session, "boss", True)
    assert anon_client.delete("/api/v1/system/users/999999",
                              headers=_hdr(anon_client, "boss")).status_code == 404


# ------------------------------------------------------------------ 建/删角色
def test_create_and_delete_custom_role(anon_client, db_session, seeded):
    _mk(db_session, "boss", True)
    hb = _hdr(anon_client, "boss")
    r = anon_client.post("/api/v1/system/roles", headers=hb,
                         json={"code": "auditor", "name": "审计员", "description": "只读审计"})
    assert r.status_code == 201 and r.json()["code"] == "auditor"
    assert r.json()["description"] == "只读审计"
    assert anon_client.post("/api/v1/system/roles", headers=hb,
                            json={"code": "auditor", "name": "重复"}).status_code == 409
    assert anon_client.post("/api/v1/system/roles", headers=hb,
                            json={"code": "Bad Code!", "name": "坏"}).status_code == 422
    role_id = r.json()["id"]
    assert anon_client.delete(f"/api/v1/system/roles/{role_id}", headers=hb).status_code == 204
    assert db_session.get(Role, role_id) is None


def test_admin_role_and_in_use_role_cannot_be_deleted(anon_client, db_session, seeded):
    boss = _mk(db_session, "boss", True)
    worker = _mk(db_session, "worker")
    hb = _hdr(anon_client, "boss")
    admin = db_session.query(Role).filter(Role.code == "admin").one()
    assert anon_client.delete(f"/api/v1/system/roles/{admin.id}", headers=hb).status_code == 400
    viewer = db_session.query(Role).filter(Role.code == "viewer").one()
    assert anon_client.put(f"/api/v1/system/users/{worker.id}/roles", headers=hb,
                           json={"roles": ["viewer"]}).status_code == 200
    r = anon_client.delete(f"/api/v1/system/roles/{viewer.id}", headers=hb)
    assert r.status_code == 400 and "user" in r.json()["detail"]
    assert anon_client.put(f"/api/v1/system/users/{worker.id}/roles", headers=hb,
                           json={"roles": []}).status_code == 200
    assert anon_client.delete(f"/api/v1/system/roles/{viewer.id}", headers=hb).status_code == 204


def test_non_superuser_cannot_manage_roles(anon_client, db_session):
    _mk(db_session, "worker")
    hw = _hdr(anon_client, "worker")
    assert anon_client.post("/api/v1/system/roles", headers=hw,
                            json={"code": "x", "name": "x"}).status_code == 403
    assert anon_client.delete("/api/v1/system/roles/1", headers=hw).status_code == 403


# ------------------------------------------------- 首次登录强制改密（代建账号 / 他人重置）
def test_created_account_must_change_password_on_first_login(anon_client, db_session, seeded):
    _mk(db_session, "boss", True)
    hb = _hdr(anon_client, "boss")
    r = anon_client.post("/api/v1/system/users", headers=hb,
                         json={"username": "newbie", "password": CREATED_PW, "roles": ["viewer"]})
    assert r.status_code == 201
    login = anon_client.post("/api/v1/auth/login",
                             json={"username": "newbie", "password": CREATED_PW})
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    # 被标记的账号调其他接口会被服务端拦下，只能先改密码
    tok = login.json()["access_token"]
    assert anon_client.get("/api/v1/devices",
                           headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    r = anon_client.post("/api/v1/auth/change-password",
                         headers={"Authorization": f"Bearer {tok}"},
                         json={"current_password": CREATED_PW, "new_password": NEW})
    assert r.status_code == 200 and r.json()["must_change_password"] is False


def test_admin_reset_forces_password_change_on_next_login(anon_client, db_session):
    _mk(db_session, "boss", True)
    worker = _mk(db_session, "worker")
    hb = _hdr(anon_client, "boss")
    assert anon_client.patch(f"/api/v1/system/users/{worker.id}", headers=hb,
                             json={"password": NEW}).status_code == 200
    login = anon_client.post("/api/v1/auth/login", json={"username": "worker", "password": NEW})
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    tok = login.json()["access_token"]
    r = anon_client.post("/api/v1/auth/change-password",
                         headers={"Authorization": f"Bearer {tok}"},
                         json={"current_password": NEW, "new_password": "Third-Strong-Pw-55"})
    assert r.status_code == 200 and r.json()["must_change_password"] is False


# ------------------------------------------------------------------ 本人改资料
def test_update_own_profile(anon_client, db_session):
    worker = _mk(db_session, "worker")
    hw = _hdr(anon_client, "worker")
    r = anon_client.patch("/api/v1/auth/me", headers=hw,
                          json={"full_name": "王小明", "email": "wang@example.com"})
    assert r.status_code == 200
    assert r.json()["full_name"] == "王小明" and r.json()["email"] == "wang@example.com"
    db_session.refresh(worker)
    assert worker.full_name == "王小明"


def test_update_own_profile_cannot_escalate(anon_client, db_session):
    worker = _mk(db_session, "worker")
    hw = _hdr(anon_client, "worker")
    r = anon_client.patch("/api/v1/auth/me", headers=hw,
                          json={"full_name": "x", "is_superuser": True,
                                "is_active": False, "password": "Hacked-Pw-123"})
    assert r.status_code == 200
    db_session.refresh(worker)
    assert worker.is_superuser is False and worker.is_active is True
    assert _login(anon_client, "worker", GOOD)[0].status_code == 200  # 原密码仍可用


def test_update_own_profile_requires_login(anon_client, db_session):
    assert anon_client.patch("/api/v1/auth/me",
                             json={"full_name": "x"}).status_code == 401
