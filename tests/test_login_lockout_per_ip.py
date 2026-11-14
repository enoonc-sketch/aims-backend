"""登录锁定按「来源 IP + 用户名」组合计数。

之前按用户名单独计数：任何人在任何地址对 admin 输错 5 次，真正的管理员（别的地址、
正确密码）也被锁在门外——等于给了别人一个“一键锁死管理员”的开关。现在每个
(IP, 用户名) 组合各算各的，锁一个不影响其他。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.v1 import auth as auth_mod
from app.core import security
from app.core.config import settings
from app.models.system import LoginFailure, User

GOOD_PW = "Correct-Horse-Battery-9"
BAD_PW = "wrong-password-1"


def _user(db, name):
    db.add(User(username=name, hashed_password=security.hash_password(GOOD_PW), is_active=True))
    db.commit()


@pytest.fixture()
def from_ip(monkeypatch):
    """让后续请求“来自”指定地址：from_ip("1.2.3.4")。"""
    state = {"ip": "10.0.0.1"}
    monkeypatch.setattr(auth_mod, "_client_ip", lambda request: state["ip"])

    def switch(ip):
        state["ip"] = ip

    return switch


def _login(c, name, pw):
    return c.post("/api/v1/auth/login", json={"username": name, "password": pw})


def test_attacker_cannot_lock_admin_out_for_the_real_admin(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 5)
    _user(db_session, "admin")

    from_ip("203.0.113.9")                       # 攻击者
    for _ in range(5):
        assert _login(anon_client, "admin", BAD_PW).status_code == 401
    assert _login(anon_client, "admin", BAD_PW).status_code == 429   # 攻击者自己被锁

    from_ip("192.168.1.20")                      # 真正的管理员，另一个地址
    assert _login(anon_client, "admin", GOOD_PW).status_code == 200


def test_locked_pair_stays_locked_even_with_correct_password(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    _user(db_session, "admin")
    from_ip("203.0.113.9")
    for _ in range(3):
        _login(anon_client, "admin", BAD_PW)
    r = _login(anon_client, "admin", GOOD_PW)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_locking_one_username_does_not_lock_other_usernames_on_same_ip(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    _user(db_session, "admin")
    _user(db_session, "bob")
    from_ip("198.51.100.7")
    for _ in range(3):
        _login(anon_client, "admin", BAD_PW)
    assert _login(anon_client, "admin", GOOD_PW).status_code == 429
    assert _login(anon_client, "bob", GOOD_PW).status_code == 200


def test_failures_from_other_address_do_not_count_toward_my_pair(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    _user(db_session, "admin")
    from_ip("203.0.113.9")
    for _ in range(2):
        _login(anon_client, "admin", BAD_PW)
    from_ip("192.168.1.20")
    for _ in range(2):                           # 我自己输错两次，加起来 4 次，但各自 < 3
        assert _login(anon_client, "admin", BAD_PW).status_code == 401
    assert _login(anon_client, "admin", GOOD_PW).status_code == 200


def test_success_only_clears_this_pair(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 5)
    _user(db_session, "admin")
    from_ip("203.0.113.9")
    _login(anon_client, "admin", BAD_PW)
    from_ip("192.168.1.20")
    _login(anon_client, "admin", BAD_PW)
    assert _login(anon_client, "admin", GOOD_PW).status_code == 200
    left = db_session.scalars(select(LoginFailure)).all()
    assert [(r.username, r.ip_address) for r in left] == [("admin", "203.0.113.9")]


def test_per_ip_spraying_ceiling_still_applies(anon_client, db_session, from_ip, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 4)
    from_ip("203.0.113.9")
    for i in range(4):
        assert _login(anon_client, f"u{i}", BAD_PW).status_code == 401
    assert _login(anon_client, "u99", BAD_PW).status_code == 429
    from_ip("192.168.1.20")                      # 别的地址不受影响
    assert _login(anon_client, "u99", BAD_PW).status_code == 401


def test_change_password_wrong_current_counts_against_pair_only(client, test_user, from_ip, monkeypatch):
    """改密码接口里输错当前密码也计入锁定，但同样只锁（IP+用户名）这一对。"""
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 2)
    body = {"current_password": "nope-nope-1A!", "new_password": "Another-Good-Pw-7"}
    from_ip("203.0.113.9")
    for _ in range(2):
        assert client.post("/api/v1/auth/change-password", json=body).status_code == 400
    assert client.post("/api/v1/auth/change-password", json=body).status_code == 429
    from_ip("192.168.1.20")
    assert client.post("/api/v1/auth/change-password", json=body).status_code == 400  # 另一个地址没被锁
