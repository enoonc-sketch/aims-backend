"""SQLAlchemy engine / session setup. Supports PostgreSQL and SQLite (standalone EXE).

Engine is created lazily so EXE entrypoint can set DATABASE_URL before first use.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    settings = get_settings()
    connect_args = {}
    engine_kwargs: dict = {"future": True}

    if settings.is_sqlite:
        connect_args["check_same_thread"] = False
        db_path = settings.sqlite_path
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        if ":memory:" in settings.DATABASE_URL:
            from sqlalchemy.pool import StaticPool
            engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs["pool_pre_ping"] = True

    engine_kwargs["connect_args"] = connect_args
    _engine = create_engine(settings.DATABASE_URL, **engine_kwargs)

    if settings.is_sqlite:

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


# Backwards-compatible module attributes used by init_db / tests
def __getattr__(name: str):
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_session_factory()
    raise AttributeError(name)


def get_db():
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
