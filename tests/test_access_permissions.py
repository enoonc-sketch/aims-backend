"""设置页“登录账号 / 权限设置”与托盘登录状态依赖的后端行为。"""
from __future__ import annotations

import time

import pytest

from app.core import security
from app.db import init_db as init_mod
from app.models.system import Role, User
from app.services import access

GOOD = "Correct-Horse-Battery-9"
NEWPW = "Another-Strong-Pw-77"


@pytest.fixture()
def seeded(db_session):
    init_mod._seed_roles(db_session)
    init_mod._seed_permissions(db_session)
    db_session.commit()
    return db_session


def _mk(db, name, superuser=False):
    u = User(username=name, hashed_password=security.hash_password(GOOD), is_active=True, is_superuser=superuser)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _hdr(c, name):
    r = c.post("/api/v1/auth/login", json={"username": name, "password": GOOD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_user_with_role_and_login(anon_client, seeded):
    _mk(seeded, "boss", True)
    hb = _hdr(anon_client, "boss")
    r = anon_client.post("/api/v1/system/users", headers=hb,
                         json={"username": "worker", "password": NEWPW, "roles": ["engineer"]})
    assert r.status_code == 201 and r.json()["roles"] == ["engineer"]
    listed = {u["username"]: u for u in anon_client.get("/api/v1/system/users", headers=hb).json()}
    assert listed["worker"]["roles"] == ["engineer"] and listed["boss"]["roles"] == []
    assert anon_client.post("/api/v1/auth/login", json={"username": "worker", "password": NEWPW}).status_code == 200


def test_unknown_role_is_rejected(anon_client, seeded):
    _mk(seeded, "boss", True)
    r = anon_client.post("/api/v1/system/users", headers=_hdr(anon_client, "boss"),
                         json={"username": "w2", "password": NEWPW, "roles": ["nope"]})
    assert r.status_code == 422


def test_non_superuser_cannot_manage_permissions(anon_client, seeded):
    _mk(seeded, "worker")
    hw = _hdr(anon_client, "worker")
    assert anon_client.get("/api/v1/system/role-permissions", headers=hw).status_code == 403
    assert anon_client.get("/api/v1/system/permissions", headers=hw).status_code == 403
    role = seeded.query(Role).filter(Role.code == "viewer").one()
    assert anon_client.put(f"/api/v1/system/roles/{role.id}/permissions", headers=hw,
                           json={"permissions": []}).status_code == 403


def test_role_permissions_edit_and_pages_in_me(anon_client, seeded):
    _mk(seeded, "boss", True); w = _mk(seeded, "worker")
    hb = _hdr(anon_client, "boss")
    # 新种子：每个现有角色默认拥有全部页面权限（升级前行为不变）
    viewer = {r["code"]: r for r in anon_client.get("/api/v1/system/role-permissions", headers=hb).json()}["viewer"]
    assert {"page.dash", "page.ai"} <= set(viewer["permissions"])
    # 分配角色，再把 viewer 收紧到只有看板和设备
    r = anon_client.put(f"/api/v1/system/users/{w.id}/roles", headers=hb, json={"roles": ["viewer"]})
    assert r.status_code == 200 and r.json()["roles"] == ["viewer"]
    r = anon_client.put(f"/api/v1/system/roles/{viewer['id']}/permissions", headers=hb,
                        json={"permissions": ["page.dash", "page.dev"]})
    assert r.status_code == 200 and r.json()["permissions"] == ["page.dash", "page.dev"]
    me = anon_client.get("/api/v1/auth/me", headers=_hdr(anon_client, "worker")).json()
    assert me["pages"] == ["dash", "dev"] and me["roles"] == ["viewer"] and "Viewer" in me["permission"]
    # 超级管理员不受限
    assert anon_client.get("/api/v1/auth/me", headers=hb).json()["pages"] is None


def test_role_less_legacy_user_is_unrestricted_and_admin_role_is_locked(anon_client, seeded):
    _mk(seeded, "boss", True); _mk(seeded, "legacy")
    assert anon_client.get("/api/v1/auth/me", headers=_hdr(anon_client, "legacy")).json()["pages"] is None
    admin_role = seeded.query(Role).filter(Role.code == "admin").one()
    r = anon_client.put(f"/api/v1/system/roles/{admin_role.id}/permissions",
                        headers=_hdr(anon_client, "boss"), json={"permissions": []})
    assert r.status_code == 400
    bad = seeded.query(Role).filter(Role.code == "viewer").one()
    r = anon_client.put(f"/api/v1/system/roles/{bad.id}/permissions",
                        headers=_hdr(anon_client, "boss"), json={"permissions": ["page.nope"]})
    assert r.status_code == 422


def test_page_seed_does_not_overwrite_admin_edits(seeded):
    from app.models.system import Permission, RolePermission
    viewer = seeded.query(Role).filter(Role.code == "viewer").one()
    seeded.query(RolePermission).filter(RolePermission.role_id == viewer.id).delete()
    seeded.commit()
    init_mod._seed_permissions(seeded); seeded.commit()          # 再启动一次
    assert seeded.query(RolePermission).filter(RolePermission.role_id == viewer.id).count() == 0


# ------------------------------------------------------------- 界面登录状态（托盘）
def test_ui_state_report_and_launcher_read(anon_client, seeded, monkeypatch):
    monkeypatch.setenv("AIMS_LAUNCHER_TOKEN", "tok-123")
    access.report_logout()
    _mk(seeded, "boss", True)
    assert anon_client.get("/api/v1/auth/ui-state", headers={"X-AIMS-Launcher": "tok-123"}).json()["logged_in"] is False
    hb = _hdr(anon_client, "boss")
    assert anon_client.post("/api/v1/auth/ui-state", headers=hb).status_code == 204
    snap = anon_client.get("/api/v1/auth/ui-state", headers={"X-AIMS-Launcher": "tok-123"}).json()
    assert snap["logged_in"] is True and snap["username"] == "boss" and "超级管理员" in snap["permission"]
    # 退出后（不带令牌上报 / 调 logout）都变成未登录
    assert anon_client.post("/api/v1/auth/ui-state").status_code == 204
    assert anon_client.get("/api/v1/auth/ui-state", headers={"X-AIMS-Launcher": "tok-123"}).json()["logged_in"] is False


def test_ui_state_read_needs_the_launcher_token(anon_client, monkeypatch):
    monkeypatch.setenv("AIMS_LAUNCHER_TOKEN", "tok-123")
    assert anon_client.get("/api/v1/auth/ui-state").status_code == 404
    assert anon_client.get("/api/v1/auth/ui-state", headers={"X-AIMS-Launcher": "wrong"}).status_code == 404
    monkeypatch.delenv("AIMS_LAUNCHER_TOKEN")
    assert anon_client.get("/api/v1/auth/ui-state", headers={"X-AIMS-Launcher": ""}).status_code == 404


def test_ui_state_expires_with_the_access_token(seeded):
    u = _mk(seeded, "boss", True)
    access.report_login(u, "x", time.time() - 1)
    assert access.snapshot()["logged_in"] is False
    access.report_login(u, "x", time.time() + 60)
    assert access.snapshot()["logged_in"] is True
    access.report_logout()


def test_logout_clears_ui_state(anon_client, seeded):
    _mk(seeded, "boss", True)
    r = anon_client.post("/api/v1/auth/login", json={"username": "boss", "password": GOOD}).json()
    hb = {"Authorization": f"Bearer {r['access_token']}"}
    anon_client.post("/api/v1/auth/ui-state", headers=hb)
    assert access.snapshot()["logged_in"] is True
    anon_client.post("/api/v1/auth/logout", headers=hb, json={"refresh_token": r["refresh_token"]})
    assert access.snapshot()["logged_in"] is False
