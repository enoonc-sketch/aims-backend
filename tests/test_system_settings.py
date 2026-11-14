"""系统设置接口：GET /system/settings、PUT /system/settings/{key}。

设置页把 UI 偏好（API 端点、刷新间隔）存在后端；读对所有登录用户开放，
写只允许超级管理员。值本身不记入审计（可能含敏感内容），只记 key。
"""
from __future__ import annotations

from app.core.security import hash_password
from app.models.system import User

ADMIN_PW = "Admin-Strong-Pw-9"
USER_PW = "User-Strong-Pw-9"


def _make_user(db, username, password, is_superuser=False):
    user = User(username=username, hashed_password=hash_password(password),
                is_active=True, is_superuser=is_superuser)
    db.add(user)
    db.commit()
    return user


def _login(c, name, pw):
    r = c.post("/api/v1/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_superuser_put_get_round_trip(anon_client, db_session):
    _make_user(db_session, "admin", ADMIN_PW, is_superuser=True)
    h = _login(anon_client, "admin", ADMIN_PW)

    r = anon_client.put("/api/v1/system/settings/ui.refresh_interval", headers=h,
                        json={"value": "120"})
    assert r.status_code == 200
    assert r.json()["key"] == "ui.refresh_interval"
    assert r.json()["value"] == "120"

    r = anon_client.get("/api/v1/system/settings/ui.refresh_interval", headers=h)
    assert r.status_code == 200
    assert r.json()["value"] == "120"

    r = anon_client.get("/api/v1/system/settings", headers=h)
    assert r.status_code == 200
    assert {s["key"]: s["value"] for s in r.json()}.get("ui.refresh_interval") == "120"

    # 再次 PUT 是更新，不是新建；不存在的 key 读不到。
    r = anon_client.put("/api/v1/system/settings/ui.refresh_interval", headers=h,
                        json={"value": "30"})
    assert r.status_code == 200 and r.json()["value"] == "30"
    assert anon_client.get("/api/v1/system/settings/no-such-key", headers=h).status_code == 404


def test_regular_user_can_read_but_not_write(anon_client, db_session):
    _make_user(db_session, "alice", USER_PW)
    h = _login(anon_client, "alice", USER_PW)
    assert anon_client.get("/api/v1/system/settings", headers=h).status_code == 200
    r = anon_client.put("/api/v1/system/settings/ui.refresh_interval", headers=h,
                        json={"value": "30"})
    assert r.status_code == 403


def test_unauthenticated_cannot_read_settings(anon_client, db_session):
    assert anon_client.get("/api/v1/system/settings").status_code == 401
    assert anon_client.put("/api/v1/system/settings/ui.refresh_interval",
                           json={"value": "30"}).status_code == 401
