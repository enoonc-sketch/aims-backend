"""Pytest fixtures for API tests — works with SQLite (default) or PostgreSQL."""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

# Prefer SQLite for local/CI tests; override with TEST_DATABASE_URL for Postgres
# SECRET_KEY has no default in the app; tests inject a throw-away random one.
import secrets as _secrets  # noqa: E402

os.environ.setdefault("SECRET_KEY", _secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENV", "development")
os.environ["CORS_ORIGINS"] = "https://ui.example.test"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("AUTO_INIT_DB", "false")
# 限流在测试里默认关闭：pytest 所有请求来自同一个 TestClient IP，
# 开启会命中 429 而不是被测功能本身。专门的限流测试自行打开。
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "TEST_DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:"),
)

# Clear settings cache so DATABASE_URL is picked up
from app.core import config as config_mod  # noqa: E402

config_mod.get_settings.cache_clear()

from app.db.base import Base  # noqa: E402
from app.api.deps import get_current_user, get_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.system import User  # noqa: E402
import app.models  # noqa: E402
from app.main import app  # noqa: E402

TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
def engine():
    connect_args = {}
    if TEST_DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    eng = create_engine(TEST_DATABASE_URL, future=True, connect_args=connect_args)
    if TEST_DATABASE_URL.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    if "postgresql" in TEST_DATABASE_URL:
        with eng.connect() as conn:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
            except Exception:
                pass
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    TestingSessionLocal = sessionmaker(bind=connection, future=True)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def test_user(db_session):
    """A real, persisted, active superuser — used to authenticate `client` by
    default so the hundreds of existing endpoint tests didn't all need to be
    rewritten to log in just because their routes started requiring auth.
    `is_superuser=True` so this same override also satisfies
    get_current_superuser for the (few) admin-only endpoints.
    """
    user = User(
        username="test-fixture-user",
        hashed_password=hash_password("Fixture-Pw-0nly-4-Tests!"),
        full_name="Test Fixture User",
        is_active=True,
        is_superuser=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture()
def client(db_session, test_user):
    """Authenticated test client: `get_current_user` is overridden to return
    `test_user` directly (skipping real JWT decoding), so every test using
    this fixture is "logged in" without needing an Authorization header.
    Use `anon_client` instead for tests that specifically exercise
    unauthenticated access (see tests/test_auth_enforcement.py).
    """
    def _override_get_db():
        yield db_session

    def _override_get_current_user():
        return test_user

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(db_session):
    """Same DB as `client`, but with real auth enforcement left in place —
    no Authorization header will pass. For asserting that write endpoints
    actually reject unauthenticated callers (and for exercising /auth/login
    for real)."""
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
