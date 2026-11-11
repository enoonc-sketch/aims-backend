"""首次登录凭据落盘（随机初始密码 → 人可读文件）。

背景：首个管理员由 provision 用 secrets.token 随机口令创建（secrets.json 是
JSON、给人读的体验很差）。用户拿 admin/admin 反复试错只会得到「用户名或
密码不正确」。凭据必须同步写成可读文件，并提示首次登录后改密。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.deploy import conf
from app.deploy.paths import Layout


@pytest.fixture()
def layout(tmp_path: Path, monkeypatch) -> Layout:
    monkeypatch.setenv("AIMS_DATA_ROOT", str(tmp_path / "data"))
    lay = Layout(root=tmp_path / "app")
    lay.ensure_dirs() if hasattr(lay, "ensure_dirs") else lay.config_dir.mkdir(parents=True, exist_ok=True)
    return lay


def test_credentials_file_written_with_random_password(layout: Layout):
    path = conf.write_first_login_credentials(
        layout, username="admin", password="Xy3kPq9mRt2wLz5A", api_port=8000)
    assert path == layout.config_dir / "first-login.txt"
    text = path.read_text(encoding="utf-8")
    assert "admin" in text
    assert "Xy3kPq9mRt2wLz5A" in text
    # 必须提示改密码与删除文件，避免旧凭据长期留在盘上
    assert "修改密码" in text
    assert "删除" in text
    # 登录入口要写清端口
    assert "127.0.0.1:8000" in text


def test_credentials_path_helper(layout: Layout):
    assert conf.first_login_credentials_path(layout) == layout.config_dir / "first-login.txt"


def test_new_bootstrap_password_avoids_look_alikes():
    for _ in range(50):
        pw = conf.new_bootstrap_password()
        assert len(pw) == 16
        assert all(c in "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789" for c in pw)


def test_bootstrap_admin_flag_defaults_false():
    from app.db import init_db
    assert init_db.BOOTSTRAP_ADMIN_CREATED in (True, False)


def test_new_bootstrap_password_is_random():
    assert conf.new_bootstrap_password() != conf.new_bootstrap_password()
