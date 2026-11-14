"""角色页“重置用户密码”所依赖的后端行为：只有超级管理员能改他人密码，且旧会话/旧密码立即失效。"""
from __future__ import annotations

from app.core import security
from app.models.system import User

GOOD = "Correct-Horse-Battery-9"
NEW = "Another-Strong-Pw-77"


def _mk(db, name, superuser=False):
    u = User(username=name, hashed_password=security.hash_password(GOOD), is_active=True, is_superuser=superuser)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _login(c, name, pw):
    r = c.post("/api/v1/auth/login", json={"username": name, "password": pw})
    return r, ({"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else {})


def test_superuser_resets_other_users_password(anon_client, db_session):
    _mk(db_session, "boss", True); worker = _mk(db_session, "worker")
    _, hw = _login(anon_client, "worker", GOOD)
    rt = anon_client.post("/api/v1/auth/login", json={"username": "worker", "password": GOOD}).json()["refresh_token"]
    _, hb = _login(anon_client, "boss", GOOD)
    r = anon_client.patch(f"/api/v1/system/users/{worker.id}", headers=hb, json={"password": NEW})
    assert r.status_code == 200
    assert _login(anon_client, "worker", GOOD)[0].status_code == 401           # 旧密码作废
    assert _login(anon_client, "worker", NEW)[0].status_code == 200            # 新密码可用
    assert anon_client.post("/api/v1/auth/refresh", json={"refresh_token": rt}).status_code == 401   # 旧会话被踢


def test_normal_user_cannot_reset_anyones_password(anon_client, db_session):
    victim = _mk(db_session, "victim"); _mk(db_session, "worker")
    _, hw = _login(anon_client, "worker", GOOD)
    assert anon_client.patch(f"/api/v1/system/users/{victim.id}", headers=hw, json={"password": NEW}).status_code == 403
    assert _login(anon_client, "victim", GOOD)[0].status_code == 200


def test_weak_password_is_rejected(anon_client, db_session):
    _mk(db_session, "boss", True); worker = _mk(db_session, "worker")
    _, hb = _login(anon_client, "boss", GOOD)
    r = anon_client.patch(f"/api/v1/system/users/{worker.id}", headers=hb, json={"password": "alllowercaseonly"})
    assert r.status_code == 422