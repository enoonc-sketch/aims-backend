"""安装版默认账号：admin / admin123（与桌面独立版空库启动一致）。

以前安装包在安装时生成 16 位随机管理员密码；现在全新安装默认 admin/admin123，用户装完
直接登录，登录后在「系统设置 → 修改密码」里改。生产环境仍然拒绝默认账号。

这里不启动真实 PostgreSQL（那属于 Windows 安装验收），而是把安装器真正会写出来的
aims.env 内容喂给应用的配置与首次建账号逻辑，验证“装完能用 admin/admin123 登录”这条链。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core import security
from app.core.config import Settings, settings
from app.db import init_db as init_mod
from app.deploy import conf, provision
from app.deploy.paths import Layout
from app.models.system import User

LONG_KEY = "k" * 8 + "Xy7-" * 12


@pytest.fixture()
def layout(tmp_path: Path, monkeypatch) -> Layout:
    monkeypatch.setenv("AIMS_DATA_ROOT", str(tmp_path / "data"))
    lay = Layout(root=tmp_path / "app")
    lay.config_dir.mkdir(parents=True, exist_ok=True)
    return lay


def _installer_env(layout: Layout, *, secrets: dict | None = None, admin_username: str = "admin") -> dict[str, str]:
    sec = {"SECRET_KEY": LONG_KEY, "DB_PASSWORD": "dbpw", "DB_SUPERUSER_PASSWORD": "supw",
           "ADMIN_PASSWORD": conf.DEFAULT_ADMIN_PASSWORD}
    sec.update(secrets or {})
    return conf.build_env(layout, {"pg": {"host": "127.0.0.1", "app_user": "aims", "database": "aims"}},
                          sec, port=55432, api_port=8000, admin_username=admin_username)


def _apply_to_global_settings(monkeypatch, env: dict[str, str]) -> None:
    """把安装器写出的与管理员相关的配置项落到应用的 settings 上（等价于启动时读 aims.env）。"""
    parsed = Settings(_env_file=None, **{k: v for k, v in env.items()
                                         if k in {"APP_ENV", "BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD",
                                                  "ALLOW_DEV_ADMIN", "DEV_ADMIN_USERNAME", "DEV_ADMIN_PASSWORD",
                                                  "SECRET_KEY"}})
    for name in ("APP_ENV", "BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD", "ALLOW_DEV_ADMIN",
                 "DEV_ADMIN_USERNAME", "DEV_ADMIN_PASSWORD"):
        monkeypatch.setattr(settings, name, getattr(parsed, name))


# --------------------------------------------------------------------------- secrets / env
def test_fresh_secrets_default_admin_password_is_admin123(layout: Layout):
    assert conf.ensure_secrets(layout)["ADMIN_PASSWORD"] == "admin123"


def test_default_install_env_does_not_inject_bootstrap_or_force_dev_admin(layout: Layout):
    env = _installer_env(layout)
    assert "BOOTSTRAP_ADMIN_USERNAME" not in env and "BOOTSTRAP_ADMIN_PASSWORD" not in env
    # 不写 ALLOW_DEV_ADMIN：以后把 APP_ENV 改成 production 时配置里没有强制开启默认账号的项
    assert "ALLOW_DEV_ADMIN" not in env
    assert env["APP_ENV"] == "development"
    text = conf.render_env(env)
    assert "BOOTSTRAP_ADMIN_PASSWORD" not in text


def test_custom_password_still_goes_through_bootstrap(layout: Layout):
    env = _installer_env(layout, secrets={"ADMIN_PASSWORD": "Rea1-Strong-Pw-42"})
    assert env["BOOTSTRAP_ADMIN_USERNAME"] == "admin"
    assert env["BOOTSTRAP_ADMIN_PASSWORD"] == "Rea1-Strong-Pw-42"
    assert "DEV_ADMIN_USERNAME" not in env


def test_legacy_random_secret_on_upgrade_keeps_bootstrap_behaviour(layout: Layout):
    legacy = conf.new_bootstrap_password()
    env = _installer_env(layout, secrets={"ADMIN_PASSWORD": legacy})
    assert env["BOOTSTRAP_ADMIN_PASSWORD"] == legacy


# --------------------------------------------------------------------------- 装完能登录
def test_installed_default_admin_can_log_in_with_admin_admin(anon_client, db_session, layout, monkeypatch):
    env = _installer_env(layout)
    _apply_to_global_settings(monkeypatch, env)
    monkeypatch.setattr(init_mod, "BOOTSTRAP_ADMIN_CREATED", False)

    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)

    assert provision._bootstrap_admin_created() is True      # provision 据此写 first-login.txt
    u = db_session.scalar(select(User).where(User.username == "admin"))
    assert u and u.is_superuser and u.is_active
    r = anon_client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200 and r.json()["access_token"]


def test_custom_admin_username_with_default_password(anon_client, db_session, layout, monkeypatch):
    env = _installer_env(layout, admin_username="ops")
    assert env["DEV_ADMIN_USERNAME"] == "ops"
    _apply_to_global_settings(monkeypatch, env)
    monkeypatch.setattr(init_mod, "BOOTSTRAP_ADMIN_CREATED", False)
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    assert [u.username for u in db_session.scalars(select(User)).all()] == ["ops"]
    r = anon_client.post("/api/v1/auth/login", json={"username": "ops", "password": "admin123"})
    assert r.status_code == 200


def test_provision_flag_is_read_live_not_copied_at_import(db_session, monkeypatch):
    """回归：provision 曾 `from init_db import BOOTSTRAP_ADMIN_CREATED`，拿到的永远是导入时的 False，
    first-login.txt 因此从未写出。"""
    monkeypatch.setattr(init_mod, "BOOTSTRAP_ADMIN_CREATED", False)
    assert provision._bootstrap_admin_created() is False
    monkeypatch.setattr(init_mod, "BOOTSTRAP_ADMIN_CREATED", True)
    assert provision._bootstrap_admin_created() is True


# --------------------------------------------------------------------------- 生产环境仍拒绝
def test_production_never_allows_the_default_account(layout: Layout):
    env = _installer_env(layout)
    prod = Settings(_env_file=None, APP_ENV="production", SECRET_KEY=LONG_KEY,
                    TRUSTED_PROXY_IPS="10.0.0.1", SEED_DEMO_DATA=False)
    assert prod.dev_admin_allowed is False
    # 同一份安装配置只把 APP_ENV 换成 production：没有任何项强制开启默认账号，应用照常构造
    cfg = {k: env[k] for k in ("SECRET_KEY",)}
    again = Settings(_env_file=None, APP_ENV="production", TRUSTED_PROXY_IPS="10.0.0.1",
                     SEED_DEMO_DATA=False, **cfg)
    assert again.dev_admin_allowed is False


def test_production_refuses_to_start_when_default_account_is_forced(layout: Layout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, APP_ENV="production", SECRET_KEY=LONG_KEY, TRUSTED_PROXY_IPS="10.0.0.1",
                 SEED_DEMO_DATA=False, ALLOW_DEV_ADMIN=True)


def test_production_bootstrap_does_not_create_default_admin(db_session, monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    monkeypatch.setattr(settings, "ALLOW_DEV_ADMIN", None)
    init_mod._seed_roles(db_session)
    init_mod._bootstrap_admin(db_session)
    assert db_session.scalar(select(User)) is None


# --------------------------------------------------------------------------- first-login.txt
def test_first_login_file_for_default_account_says_admin_admin(layout: Layout):
    path = conf.write_first_login_credentials(layout, username="admin", password="admin123", api_port=8000)
    text = path.read_text(encoding="utf-8")
    assert "用户名：admin" in text and "初始密码：admin123" in text
    assert "系统设置 → 修改密码" in text
    assert "随机" not in text
    assert "127.0.0.1:8000" in text


def test_legacy_default_password_admin_is_still_treated_as_default(layout: Layout):
    """旧版 secrets.json 里存的默认口令是 admin：重新 provision 不能把它当“自定义口令”去走强度校验而起不来。"""
    env = _installer_env(layout, secrets={"ADMIN_PASSWORD": "admin"})
    assert "BOOTSTRAP_ADMIN_PASSWORD" not in env
    path = conf.write_first_login_credentials(layout, username="admin", password="admin", api_port=8000)
    assert "初始密码：admin123" in path.read_text(encoding="utf-8")
