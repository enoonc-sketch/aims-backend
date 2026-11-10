"""Backup listing / restore commands and the Windows app mutex (0.5.9)."""
import importlib.util
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def rs():
    spec = importlib.util.spec_from_file_location("run_standalone_restore", _ROOT / "run_standalone.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mkdb(path, value):
    con = sqlite3.connect(path)
    con.execute("create table if not exists t(x)")
    con.execute("delete from t")
    con.execute("insert into t values (?)", (value,))
    con.commit()
    con.close()


def _value(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("select x from t").fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def env(tmp_path, monkeypatch, rs):
    data = tmp_path / "aims_data"
    (data / "backups").mkdir(parents=True)
    db = data / "aims.db"
    _mkdb(db, "current")
    _mkdb(data / "backups" / "aims-20260101-010101.db", "old")
    _mkdb(data / "backups" / "aims-20260102-010101.db", "newer")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setattr(rs, "_probe_health", lambda *a, **k: None)  # AIMS not running
    return db


def test_list_backups_newest_first_and_ignores_safety_copies(rs, env):
    (env.parent / "backups" / "pre-restore-20260103-000000.db").write_bytes(b"x")
    assert [b.name for b in rs._list_backups()] == ["aims-20260102-010101.db", "aims-20260101-010101.db"]


def test_restore_latest_replaces_db_and_keeps_safety_copy(rs, env, capsys):
    assert rs._restore_backup("latest") == 0
    assert _value(env) == "newer"
    safety = list((env.parent / "backups").glob("pre-restore-*.db"))
    assert len(safety) == 1 and _value(safety[0]) == "current"
    assert len(rs._list_backups()) == 2  # safety copy is not counted as an automatic backup
    assert "Restored aims-20260102-010101.db" in capsys.readouterr().out


def test_restore_named_backup(rs, env):
    assert rs._restore_backup("aims-20260101-010101.db") == 0
    assert _value(env) == "old"


def test_restore_refuses_while_running(rs, env, monkeypatch):
    monkeypatch.setattr(rs, "_probe_health", lambda *a, **k: {"app": "AIMS Backend"})
    assert rs._restore_backup("latest") == 1
    assert _value(env) == "current"


def test_restore_rejects_corrupt_backup_and_unknown_name(rs, env):
    bad = env.parent / "backups" / "aims-20260109-000000.db"
    bad.write_bytes(b"this is not a sqlite file" * 100)
    assert rs._restore_backup("latest") == 1  # newest is the corrupt one
    assert _value(env) == "current" and not list((env.parent / "backups").glob("pre-restore-*.db"))
    assert rs._restore_backup("nope.db") == 1


def test_restore_without_backups_or_non_sqlite(rs, tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_probe_health", lambda *a, **k: None)
    db = tmp_path / "aims.db"
    _mkdb(db, "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + db.as_posix())
    assert rs._restore_backup() == 1
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert rs._restore_backup() == 1


def test_cli_subcommands(rs, env, monkeypatch, capsys):
    monkeypatch.setattr(rs, "_prepare_env", lambda base: None)
    monkeypatch.setattr(sys, "argv", ["AIMS-Backend.exe", "backups"])
    assert rs.main() == 0
    assert "aims-20260102-010101.db" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["AIMS-Backend.exe", "restore", "aims-20260101-010101.db"])
    assert rs.main() == 0 and _value(env) == "old"


def test_app_mutex_only_on_windows(rs, monkeypatch):
    assert rs._hold_app_mutex() is None  # this sandbox is not Windows
    calls = []
    fake = types.ModuleType("ctypes")
    fake.windll = types.SimpleNamespace(kernel32=types.SimpleNamespace(
        CreateMutexW=lambda sec, owner, name: calls.append(name) or 4242))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(rs.os, "name", "nt")
    assert rs._hold_app_mutex() == 4242
    assert calls == ["AIMS-Backend-Mutex"]
    assert "AppMutex=AIMS-Backend-Mutex" in (_ROOT / "installer" / "AIMS-Backend.iss").read_text(encoding="utf-8-sig")
