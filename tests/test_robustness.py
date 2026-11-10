"""Startup backup, file logging and favicon (0.5.8)."""
import importlib.util
import logging
import sqlite3
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def rs():
    spec = importlib.util.spec_from_file_location("run_standalone_robust", _ROOT / "run_standalone.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "aims_data" / "aims.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute("create table t(x)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + path.as_posix())
    monkeypatch.delenv("AIMS_BACKUP_KEEP", raising=False)
    return path


def test_backup_makes_a_valid_copy(rs, db):
    dest = rs._backup_database()
    assert dest is not None and dest.parent == db.parent / "backups"
    assert sqlite3.connect(dest).execute("select x from t").fetchall() == [(42,)]


def test_backup_at_most_once_per_12h_and_prunes(rs, db, monkeypatch):
    monkeypatch.setenv("AIMS_BACKUP_KEEP", "2")
    t0 = time.time()
    first = rs._backup_database(now=t0)
    assert first is not None
    assert rs._backup_database(now=t0 + 3600) is None  # too soon
    for i in range(1, 4):  # three more, each 13 h apart
        assert rs._backup_database(now=t0 + i * 13 * 3600) is not None
    names = sorted(p.name for p in (db.parent / "backups").glob("aims-*.db"))
    assert len(names) == 2 and first.name not in names  # oldest pruned, newest kept


@pytest.mark.parametrize("keep", ["0", ""])
def test_backup_can_be_disabled(rs, db, monkeypatch, keep):
    monkeypatch.setenv("AIMS_BACKUP_KEEP", keep)
    assert rs._backup_database() is None
    assert not (db.parent / "backups").exists()


def test_backup_ignores_non_sqlite_and_missing_db(rs, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert rs._backup_database() is None
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + (tmp_path / "nope.db").as_posix())
    assert rs._backup_database() is None
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert rs._backup_database() is None


def test_file_logging_writes_and_does_not_duplicate(rs, db, tmp_path):
    handler = None
    try:
        handler = rs._setup_file_logging(tmp_path)
        assert handler is not None
        rs._setup_file_logging(tmp_path)  # second call must not attach another handler
        logging.getLogger("aims.test").warning("hello-from-test")
        logging.getLogger("uvicorn.error").warning("uvicorn-line")
        handler.flush()
        text = (db.parent / "logs" / "aims.log").read_text(encoding="utf-8")
        assert text.count("hello-from-test") == 1 and text.count("uvicorn-line") == 1
    finally:
        for name in ("", "uvicorn", "uvicorn.access"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                if getattr(h, "_aims_file", False):
                    lg.removeHandler(h)
                    h.close()


def test_file_logging_can_be_disabled(rs, db, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMS_LOG_FILE", "0")
    assert rs._setup_file_logging(tmp_path) is None
    assert not (db.parent / "logs").exists()


def test_favicon_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/x-icon")
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header


def test_ui_links_the_favicon(client):
    assert 'rel="icon"' in client.get("/ui").text
