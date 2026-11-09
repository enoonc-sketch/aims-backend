"""初始化向导: 一步生成密钥 / 证书 / 管理员, 且幂等、不覆盖已有密钥、不产生带 BOM 的 .env。"""
import pytest
from cryptography import x509
from sqlalchemy import func, select

from app.cli import setup as wiz
from app.core.config import Settings
from app.core.secret_rules import is_acceptable_secret
from app.core.security import verify_password
from app.models.system import User

PW = "wizard-test-passphrase-2026"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # 环境变量优先级高于 .env: 不隔离的话测试会盖住 .env 里写的值(conftest 为 TestClient 设了 REQUIRE_HTTPS=false)
    for k in ("SSL_CERTFILE", "SSL_KEYFILE", "REQUIRE_HTTPS", "APP_ENV"):
        monkeypatch.delenv(k, raising=False)
    # 向导"进程环境里已有合格密钥就不动": 测试要覆盖"没有"的情形, 所以先拿掉测试夹具注入的那个
    monkeypatch.delenv("SECRET_KEY", raising=False)


def test_first_run_generates_a_valid_secret_and_a_dev_cert(tmp_path):
    r = wiz.prepare_files(tmp_path)
    env = wiz.read_env(tmp_path / ".env")
    assert r.secret_created and is_acceptable_secret(env["SECRET_KEY"]) and r.secret == env["SECRET_KEY"]
    assert r.tls == "dev_cert" and env["SSL_CERTFILE"] == "certs/dev-cert.pem"
    cert = x509.load_pem_x509_certificate((tmp_path / "certs" / "dev-cert.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert not (tmp_path / ".env").read_bytes().startswith(b"\xef\xbb\xbf")


def test_generated_env_is_accepted_by_the_real_settings(tmp_path):
    wiz.prepare_files(tmp_path)
    s = Settings(_env_file=str(tmp_path / ".env"))
    # 0.5.0 的 REQUIRE_HTTPS 默认是 None(开发环境不强制); 向导生成的证书让
    # tls_direct 为真, 服务会直接以 HTTPS 提供。secure 包断言 REQUIRE_HTTPS is True
    # 是因为它默认 REQUIRE_HTTPS=true, 在 0.5.0 的语义下改断言为 tls_direct。
    assert s.SSL_CERTFILE == "certs/dev-cert.pem" and s.tls_direct


def test_http_local_mode_is_development_only(tmp_path):
    r = wiz.prepare_files(tmp_path, http_local=True)
    env = wiz.read_env(tmp_path / ".env")
    assert r.tls == "http_local" and env["APP_ENV"] == "development" and env["REQUIRE_HTTPS"] == "false"
    assert not (tmp_path / "certs").exists()
    assert Settings(_env_file=str(tmp_path / ".env")).REQUIRE_HTTPS is False


def test_second_run_keeps_the_existing_secret(tmp_path):
    first = wiz.prepare_files(tmp_path)
    second = wiz.prepare_files(tmp_path)
    assert not second.secret_created and second.secret == first.secret
    assert wiz.read_env(tmp_path / ".env")["SECRET_KEY"] == first.secret


def test_placeholder_secret_is_replaced_and_other_lines_survive(tmp_path):
    (tmp_path / ".env").write_text("# my notes\nPORT=9000\nSECRET_KEY=change-me-to-a-random-secret\nAI_PROVIDER=openai\n", encoding="utf-8")
    r = wiz.prepare_files(tmp_path, http_local=True)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    env = wiz.read_env(tmp_path / ".env")
    assert r.secret_created and "占位符" in r.secret_replaced_reason and is_acceptable_secret(env["SECRET_KEY"])
    assert "# my notes" in text and env["PORT"] == "9000" and env["AI_PROVIDER"] == "openai"
    assert text.count("SECRET_KEY=") == 1


def test_bom_env_from_windows_notepad_is_read_and_rewritten_clean(tmp_path):
    good = "k" * 8 + "abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbf" + f"SECRET_KEY={good}\nPORT=9001\n".encode())
    r = wiz.prepare_files(tmp_path, http_local=True)
    assert not r.secret_created and r.secret == good  # BOM 没有让它误判成"没有密钥"
    assert not (tmp_path / ".env").read_bytes().startswith(b"\xef\xbb\xbf")
    assert wiz.read_env(tmp_path / ".env")["PORT"] == "9001"


def test_externally_injected_secret_is_not_copied_into_the_env_file(tmp_path, monkeypatch):
    ext = "external-injected-secret-value-0123456789-abcdef"
    monkeypatch.setenv("SECRET_KEY", ext)
    r = wiz.prepare_files(tmp_path, http_local=True)
    assert r.secret == ext and not r.secret_created
    assert "SECRET_KEY" not in wiz.read_env(tmp_path / ".env")


def test_declared_proxy_tls_is_respected(tmp_path):
    (tmp_path / ".env").write_text("REQUIRE_HTTPS=true\n", encoding="utf-8")
    r = wiz.prepare_files(tmp_path)
    assert r.tls == "external" and not (tmp_path / "certs").exists()


def test_first_admin_is_created_once_with_a_hashed_password(db_session):
    assert wiz.create_first_admin_if_needed(db_session, "ops", PW) is True
    u = db_session.scalar(select(User).where(User.username == "ops"))
    assert u.is_superuser and u.is_active and verify_password(PW, u.hashed_password)
    assert wiz.create_first_admin_if_needed(db_session, "someone-else", PW) is False  # 已有用户: 幂等
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("pw", ["admin123", "short", "aaaaaaaaaaaaaaaaaa"])
def test_weak_admin_password_is_refused_and_nothing_is_created(db_session, pw):
    with pytest.raises(wiz.SetupError):
        wiz.create_first_admin_if_needed(db_session, "ops", pw)
    assert db_session.scalar(select(func.count()).select_from(User)) == 0


def test_cli_end_to_end_non_interactive(tmp_path, monkeypatch, db_session):
    """走真正的 main(): 用注入的会话工厂避免碰全局引擎。"""
    import app.db.session as sess
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(sess, "get_session_factory", lambda: sessionmaker(bind=db_session.get_bind(), future=True))
    monkeypatch.setattr("app.db.init_db.create_tables", lambda: None)
    monkeypatch.setenv("WIZ_PW", PW)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for k in ("APP_ENV", "REQUIRE_HTTPS"):
        monkeypatch.setenv(k, {"APP_ENV": "test", "REQUIRE_HTTPS": "false"}[k])  # main() 会改环境; 让 monkeypatch 负责还原
    monkeypatch.setenv("SECRET_KEY", "restore-me-0123456789-abcdefghijklmnopqrstuvwxyz")
    rc = wiz.main(["--http-local", "--admin-user", "ops", "--password-env", "WIZ_PW"], base=tmp_path)
    assert rc == 0
    assert db_session.scalar(select(User).where(User.username == "ops")) is not None
    # 密码不会出现在任何输出的文件里
    assert PW not in (tmp_path / ".env").read_text(encoding="utf-8")
