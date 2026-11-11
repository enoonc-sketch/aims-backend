"""管理员端点 RBAC 回归（阶段 9 安全测试发现的两个真实缺口）。

安全审计工具（tools/security_audit.py）在真实实例上发现：
  * 普通用户 GET /system/users 返回 200 —— 能枚举全部用户名、邮箱、超管标志；
  * 普通用户 POST /ai/cores/scan-local 返回 200 —— 能改动本机模型注册表。

两者都不是"越权写数据"那么刺眼，但足以拼出攻击面（谁是谁、哪些账号是超管），
且与"模型加载/卸载/删除仅超管"的既定策略不一致。此文件把修复锁死。
"""
from app.core.security import hash_password
from app.models.system import User


def _user(db_session, username, is_superuser):
    u = User(
        username=username,
        hashed_password=hash_password("Audit!Pw-2026#x"),
        full_name=username,
        is_active=True,
        is_superuser=is_superuser,
    )
    db_session.add(u)
    db_session.flush()
    return u


def _token(anon_client, username, password="Audit!Pw-2026#x"):
    r = anon_client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_user_list_requires_superuser(anon_client, db_session):
    _user(db_session, "rbac-regular", False)
    _user(db_session, "rbac-admin", True)

    regular = _token(anon_client, "rbac-regular")
    resp = anon_client.get("/api/v1/system/users", headers=regular)
    assert resp.status_code == 403, f"普通用户不应能枚举用户清单：{resp.status_code}"

    admin = _token(anon_client, "rbac-admin")
    resp = anon_client.get("/api/v1/system/users", headers=admin)
    assert resp.status_code == 200
    assert any(u["username"] == "rbac-admin" for u in resp.json())


def test_model_scan_requires_superuser(anon_client, db_session, tmp_path, monkeypatch):
    """扫描模型目录会写入模型注册表，属于管理员操作。"""
    monkeypatch.setattr("app.core.config.settings.MODELS_DIR", str(tmp_path))
    _user(db_session, "rbac-scan-regular", False)
    _user(db_session, "rbac-scan-admin", True)

    regular = _token(anon_client, "rbac-scan-regular")
    resp = anon_client.post("/api/v1/ai/cores/scan-local", headers=regular)
    assert resp.status_code == 403, f"普通用户不应能触发模型扫描：{resp.status_code}"

    admin = _token(anon_client, "rbac-scan-admin")
    resp = anon_client.post("/api/v1/ai/cores/scan-local", headers=admin)
    assert resp.status_code == 200, resp.text


def test_read_only_settings_stay_available_to_users(anon_client, db_session):
    """收紧权限不能顺手把普通用户需要的只读配置也关掉（避免功能回归）。"""
    _user(db_session, "rbac-settings-user", False)
    headers = _token(anon_client, "rbac-settings-user")
    assert anon_client.get("/api/v1/system/settings", headers=headers).status_code == 200
    assert anon_client.get("/api/v1/system/components", headers=headers).status_code == 200


def test_audit_log_is_superuser_only(anon_client, db_session):
    _user(db_session, "rbac-audit-user", False)
    headers = _token(anon_client, "rbac-audit-user")
    assert anon_client.get("/api/v1/system/audit-logs", headers=headers).status_code == 403
