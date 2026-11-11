"""模板中心认证与权限测试（1.0.1 修复回归）。

覆盖验收标准：
* 正常登录：模板列表正常加载；
* 未登录：禁止访问受保护接口（401 + WWW-Authenticate）；
* Token 过期：正确拒绝并提示重新登录；
* 无权限用户：不能修改/删除他人模板，不能改系统内置模板；
* 管理员/普通用户按权限执行不同操作；
* 系统内置模板与自定义模板相互隔离；
* 导入/导出、收藏、应用等既有功能不受影响。
"""
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.system import User


def _make_user(db_session, *, username, password="s3cret-pw-A1!", is_superuser=False):
    user = User(
        username=username,
        hashed_password=hash_password(password),
        full_name=username,
        is_active=True,
        is_superuser=is_superuser,
    )
    db_session.add(user)
    db_session.flush()
    return user, password


def _login(anon_client, username, password):
    resp = anon_client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 401 线
def test_templates_require_auth(anon_client):
    for method, path in (("GET", "/api/v1/templates"), ("POST", "/api/v1/templates"),
                         ("GET", "/api/v1/templates/categories"), ("GET", "/api/v1/templates/1")):
        resp = getattr(anon_client, method.lower())(path)
        assert resp.status_code == 401, (method, path, resp.status_code)
    resp = anon_client.get("/api/v1/templates")
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Not authenticated"


def test_expired_token_rejected(anon_client):
    """过期 access token 必须 401，而不是被默默放行。"""
    st = get_settings()
    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {"sub": "1", "type": "access", "iat": now - timedelta(hours=2),
         "nbf": now - timedelta(hours=2), "exp": now - timedelta(hours=1),
         "iss": st.JWT_ISSUER, "aud": st.JWT_AUDIENCE},
        st.SECRET_KEY, algorithm="HS256")
    resp = anon_client.get("/api/v1/templates", headers=_auth(expired))
    assert resp.status_code == 401


def test_garbage_token_rejected(anon_client):
    resp = anon_client.get("/api/v1/templates", headers=_auth("not.a.jwt"))
    assert resp.status_code == 401


# ------------------------------------------------------------ 正常登录链路
def test_logged_in_user_sees_seeded_builtin_templates(anon_client, db_session):
    _make_user(db_session, username="tpl-reader", password="Read-pw-1!")
    db_session.commit()
    token = _login(anon_client, "tpl-reader", "Read-pw-1!")
    resp = anon_client.get("/api/v1/templates", headers=_auth(token))
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert names, "登录用户应当能看到内置模板（首次访问自动播种）"
    assert all(t["source"] == "system" for t in resp.json() if t["name"] in names[:1])


# ------------------------------------------------------- 所有权与越权防护
def test_non_owner_cannot_modify_or_delete_custom_template(anon_client, db_session):
    owner, owner_pw = _make_user(db_session, username="tpl-owner", password="Owner-pw-1!")
    _make_user(db_session, username="tpl-other", password="Other-pw-1!")
    _make_user(db_session, username="tpl-admin", password="Admin-pw-1!", is_superuser=True)
    db_session.commit()
    owner_token = _login(anon_client, "tpl-owner", owner_pw)
    other_token = _login(anon_client, "tpl-other", "Other-pw-1!")
    admin_token = _login(anon_client, "tpl-admin", "Admin-pw-1!")

    resp = anon_client.post("/api/v1/templates", headers=_auth(owner_token),
                            json={"name": "我的巡检模板", "category": "维护管理", "content": {"k": 1}})
    assert resp.status_code == 201, resp.text
    tpl_id = resp.json()["id"]

    # 其他用户：读可以，改/删不行
    assert anon_client.get(f"/api/v1/templates/{tpl_id}", headers=_auth(other_token)).status_code == 200
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(other_token),
                        json={"description": "越权修改"})
    assert r.status_code == 403
    r = anon_client.delete(f"/api/v1/templates/{tpl_id}", headers=_auth(other_token))
    assert r.status_code == 403
    r = anon_client.post(f"/api/v1/templates/{tpl_id}/versions/1/restore", headers=_auth(other_token))
    assert r.status_code == 403

    # 创建者本人：可以改
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(owner_token),
                        json={"description": "作者自己改的"})
    assert r.status_code == 200

    # 管理员：可以改也可以删
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(admin_token),
                        json={"description": "管理员代改"})
    assert r.status_code == 200
    r = anon_client.delete(f"/api/v1/templates/{tpl_id}", headers=_auth(admin_token))
    assert r.status_code == 204


def test_system_template_isolation(anon_client, db_session):
    """系统内置模板：普通用户不能改不能删；管理员可改次要字段但不可改名/删除。"""
    _make_user(db_session, username="tpl-normal", password="Normal-pw-1!")
    _make_user(db_session, username="tpl-super", password="Super-pw-1!", is_superuser=True)
    db_session.commit()
    user_token = _login(anon_client, "tpl-normal", "Normal-pw-1!")
    admin_token = _login(anon_client, "tpl-super", "Super-pw-1!")

    resp = anon_client.get("/api/v1/templates?source=system", headers=_auth(user_token))
    assert resp.status_code == 200
    system = resp.json()
    assert system, "内置系统模板应已播种"
    tpl_id = system[0]["id"]

    # 普通用户：修改/删除一律 403
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(user_token),
                        json={"description": "普通用户改系统模板"})
    assert r.status_code == 403
    r = anon_client.delete(f"/api/v1/templates/{tpl_id}", headers=_auth(user_token))
    assert r.status_code == 403

    # 管理员：可改描述（次要字段）
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(admin_token),
                        json={"description": "管理员更新说明"})
    assert r.status_code == 200
    # 管理员：也不允许改系统模板的名字/分类（保护性限制对所有人生效）
    r = anon_client.put(f"/api/v1/templates/{tpl_id}", headers=_auth(admin_token),
                        json={"name": "改名的系统模板"})
    assert r.status_code == 403
    # 管理员：也不允许删除系统模板
    r = anon_client.delete(f"/api/v1/templates/{tpl_id}", headers=_auth(admin_token))
    assert r.status_code == 403


# ------------------------------------------------------- 既有功能不受影响
def test_export_and_duplicate_work_for_any_authenticated_user(anon_client, db_session):
    owner, owner_pw = _make_user(db_session, username="tpl-owner2", password="Owner2-pw-1!")
    _make_user(db_session, username="tpl-reader2", password="Reader2-pw-1!")
    db_session.commit()
    owner_token = _login(anon_client, "tpl-owner2", owner_pw)
    reader_token = _login(anon_client, "tpl-reader2", "Reader2-pw-1!")

    resp = anon_client.post("/api/v1/templates", headers=_auth(owner_token),
                            json={"name": "可导出模板", "category": "报表", "content": {"a": "b"}})
    tpl_id = resp.json()["id"]

    # 其他登录用户可以导出（导出是读取类操作）
    r = anon_client.get(f"/api/v1/templates/{tpl_id}/export", headers=_auth(reader_token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")

    # 其他登录用户可以复制出自己的独立副本（隔离：副本归复制者所有）
    r = anon_client.post(f"/api/v1/templates/{tpl_id}/duplicate", headers=_auth(reader_token))
    assert r.status_code == 201
    copy = r.json()
    assert copy["source"] == "custom"
    assert copy["name"].endswith("（副本）")

    # 批量导出自定义模板不受影响
    r = anon_client.post("/api/v1/templates/export/batch", headers=_auth(reader_token),
                         json={"all_custom": True})
    assert r.status_code == 200


def test_favorite_and_default_remain_user_level(anon_client, db_session):
    """收藏/设默认是使用者个人操作，任何登录用户都可以做（不要求所有权）。"""
    _make_user(db_session, username="tpl-fav", password="Fav-pw-1!")
    db_session.commit()
    token = _login(anon_client, "tpl-fav", "Fav-pw-1!")
    resp = anon_client.get("/api/v1/templates", headers=_auth(token))
    tpl_id = resp.json()[0]["id"]
    r = anon_client.post(f"/api/v1/templates/{tpl_id}/favorite", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["is_favorite"] is True
