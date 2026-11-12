"""Alembic 迁移必须能在"全新空库"上一路升到 head，并且可以重复执行。

背景：001_initial 用 Base.metadata.create_all 按当前模型建表，所以全新库一步就已经是最新结构；
应用启动时的自动建表也会补齐缺表。后面的迁移如果用裸的 op.create_table / op.create_index，
就会在全新库上报 DuplicateTable（007、009 就出过这个问题，README / SECURITY.md 都要求用户
执行 `alembic upgrade head`，但此前没有任何测试跑过 Alembic）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_file: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_file}", APP_ENV="development")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180
    )


def test_upgrade_head_on_empty_database_and_is_repeatable(tmp_path):
    db = tmp_path / "fresh.db"
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-1500:]
    cur = _alembic(db, "current")
    assert "(head)" in (cur.stdout + cur.stderr), cur.stdout + cur.stderr
    # 再来一次：已经在 head，必须无事发生
    again = _alembic(db, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-1500:]


def test_upgrade_when_app_already_created_tables(tmp_path):
    """升级代码后应用启动会先 create_all 补新表，用户随后按文档执行迁移：不能因为"表已存在"失败。"""
    db = tmp_path / "preexisting.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}", APP_ENV="development")
    pre = subprocess.run(
        [sys.executable, "-c", "from app.db.base import Base; import app.models; "
         "from app.db.session import engine; Base.metadata.create_all(engine)"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert pre.returncode == 0, pre.stderr[-800:]
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-1500:]
