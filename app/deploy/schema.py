"""Idempotent schema reconciliation — the data-safety core of覆盖升级.

Why not Alembic in the installed product? Because the shipped EXE must be able to
upgrade a customer's database with no Python, no pip, and no chance to debug: the
migrations directory plus a live alembic upgrade is one more thing that can fail
half-way on a machine we never see. Alembic stays the tool for developing the
schema; the deployed product reconciles instead:

    create_all()                      -> creates tables that do not exist yet
    ADD COLUMN for every missing one  -> adds columns introduced by a new version
    CREATE INDEX for missing indexes  -> adds indexes introduced by a new version

Every step is additive and skipped when already present, so running it on a
current database is a no-op, and running it on an older database only ever *adds*
objects. Existing rows are never rewritten, never dropped.

The same code path runs for SQLite (legacy/portable mode) and PostgreSQL, which is
why the SQLite "add the missing columns by hand" fixups that used to live in
app/db/init_db.py keep working — they are now a special case of this general one.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex

logger = logging.getLogger("aims.deploy.schema")

# Tables/objects that must never be touched by the column reconciler.
_SKIP_TABLES = {"alembic_version", "spatial_ref_sys"}


def _quote(engine: Engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def _column_ddl(engine: Engine, column) -> str | None:
    """ALTER TABLE ... ADD COLUMN fragment for one model column, or None if unsupported."""
    if getattr(column, "computed", None) is not None:
        return None  # generated columns cannot be added portably
    parts = [_quote(engine, column.name), column.type.compile(dialect=engine.dialect)]
    default_sql = None
    if column.server_default is not None:
        arg = getattr(column.server_default, "arg", None)
        if arg is not None:
            try:
                default_sql = str(arg.compile(dialect=engine.dialect))
            except AttributeError:
                default_sql = str(arg)
            if default_sql.upper().startswith("GENERATED"):
                default_sql = None
    if default_sql:
        parts.append(f"DEFAULT {default_sql}")
    # A new NOT NULL column without a default cannot be added to a table that may
    # already hold rows; add it nullable and report it so the caller can decide.
    if not column.nullable and default_sql:
        parts.append("NOT NULL")
    for fk in column.foreign_keys:
        target_table = _quote(engine, fk.column.table.name)
        target_col = _quote(engine, fk.column.name)
        ref = f" REFERENCES {target_table}({target_col})"
        if fk.ondelete:
            ref += f" ON DELETE {fk.ondelete.upper()}"
        parts.append(ref)
    return " ".join(parts)


def _existing_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def reconcile(engine: Engine, *, dry_run: bool = False) -> dict[str, Any]:
    """Bring the database up to the schema described by app/models.

    Returns a report dict that is logged, saved in deployment.json and printed by
    `deploy doctor`, e.g.::

        {"created_tables": [], "added_columns": ["work_orders.sla_hours"],
         "added_indexes": [...], "warnings": [...]}
    """
    import app.models  # noqa: F401  (populate Base.metadata)
    from app.db.base import Base

    report: dict[str, Any] = {
        "created_tables": [], "added_columns": [], "added_indexes": [],
        "warnings": [], "dry_run": dry_run,
    }

    before = _existing_tables(engine)
    if not dry_run:
        Base.metadata.create_all(bind=engine)
    after = _existing_tables(engine) if not dry_run else before
    report["created_tables"] = sorted(after - before)

    inspector = inspect(engine)
    # Re-create the inspector's cache: create_all just added tables.
    tables_now = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name in _SKIP_TABLES or table.name not in tables_now:
            continue
        try:
            have = {c["name"] for c in inspector.get_columns(table.name)}
        except Exception as exc:  # noqa: BLE001
            report["warnings"].append(f"读取 {table.name} 结构失败：{exc}")
            continue
        for column in table.columns:
            if column.name in have:
                continue
            ddl = _column_ddl(engine, column)
            if ddl is None:
                report["warnings"].append(f"{table.name}.{column.name} 为生成列，需人工迁移")
                continue
            stmt = f"ALTER TABLE {_quote(engine, table.name)} ADD COLUMN {ddl}"
            if not column.nullable and column.server_default is None:
                report["warnings"].append(
                    f"{table.name}.{column.name} 新增为可空列（原定义 NOT NULL 且无默认值）")
            report["added_columns"].append(f"{table.name}.{column.name}")
            if dry_run:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception as exc:  # noqa: BLE001
                report["warnings"].append(f"添加列失败 {table.name}.{column.name}: {exc}")

    # ---- indexes -----------------------------------------------------------
    try:
        existing_indexes = {
            (t, ix["name"])
            for t in tables_now
            for ix in (inspector.get_indexes(t) or [])
        }
    except Exception:  # noqa: BLE001
        existing_indexes = set()
    for table in Base.metadata.sorted_tables:
        if table.name in _SKIP_TABLES or table.name not in tables_now:
            continue
        for index in table.indexes:
            if (table.name, index.name) in existing_indexes:
                continue
            report["added_indexes"].append(index.name or f"ix_{table.name}")
            if dry_run:
                continue
            try:
                ddl = str(CreateIndex(index).compile(dialect=engine.dialect))
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception as exc:  # noqa: BLE001
                report["warnings"].append(f"创建索引失败 {index.name}: {exc}")

    if report["added_columns"] or report["created_tables"] or report["added_indexes"]:
        logger.info("Schema reconciled: %s new tables, %s new columns, %s new indexes",
                    len(report["created_tables"]), len(report["added_columns"]), len(report["added_indexes"]))
    return report


# --------------------------------------------------------------------------- #
# PostgreSQL-only extras (things create_all cannot express)
# --------------------------------------------------------------------------- #
def _has_extension(conn, name: str) -> bool:
    try:
        return conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = :n"), {"n": name}).first() is not None
    except Exception:  # noqa: BLE001
        return False


def ensure_postgres_extras(engine: Engine) -> dict[str, Any]:
    """TimescaleDB hypertable + pgvector index, with a plain-PostgreSQL fallback.

    The Windows build ships pgvector but *not* TimescaleDB (it has no supported
    Windows build), so `measurements` stays a regular table there. To keep the
    time-series queries fast anyway we add a BRIN index on `time`, which is the
    standard substitute for an append-only time column.
    """
    report: dict[str, Any] = {"hypertable": False, "vector_index": False,
                              "brin_index": False, "notes": []}
    if engine.dialect.name != "postgresql":
        return report

    with engine.connect() as conn:
        has_timescale = _has_extension(conn, "timescaledb")
        has_vector = _has_extension(conn, "vector")
        is_hypertable = False
        if has_timescale:
            try:
                is_hypertable = conn.execute(text(
                    "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'measurements'"
                )).first() is not None
            except Exception:  # noqa: BLE001
                is_hypertable = False

    if has_timescale and not is_hypertable:
        try:
            with engine.begin() as conn:
                conn.execute(text("SELECT create_hypertable('measurements', 'time', if_not_exists => TRUE)"))
            report["hypertable"] = True
        except Exception as exc:  # noqa: BLE001
            report["notes"].append(f"hypertable 转换失败：{exc}")

    if not report["hypertable"]:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_measurements_time_brin "
                    "ON measurements USING brin (time) WITH (pages_per_range = 32)"
                ))
            report["brin_index"] = True
        except Exception as exc:  # noqa: BLE001
            report["notes"].append(f"BRIN 索引创建失败：{exc}")

    if has_vector:
        try:
            with engine.begin() as conn:
                # Only meaningful when the ORM actually mapped a vector column.
                udt = conn.execute(text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'knowledge_chunks' AND column_name = 'embedding'"
                )).first()
                if udt and udt[0] == "vector":
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding "
                        "ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                    ))
                    report["vector_index"] = True
                else:
                    report["notes"].append("embedding 列不是 vector 类型（EMBEDDING_STORAGE=json），跳过 ivfflat")
        except Exception as exc:  # noqa: BLE001
            report["notes"].append(f"ivfflat 索引创建失败：{exc}")
    else:
        report["notes"].append("未安装 pgvector 扩展：知识库向量检索将使用 JSON + 应用层余弦相似度")
    return report


def table_counts(engine: Engine, limit: int = 12) -> dict[str, int]:
    """Row counts of the biggest tables — used by `doctor` / support reports."""
    out: dict[str, int] = {}
    try:
        insp = inspect(engine)
        for name in insp.get_table_names():
            if name in _SKIP_TABLES or name.startswith("knowledge_chunks_fts"):
                continue
            try:
                with engine.connect() as conn:
                    out[name] = int(conn.execute(text(f"SELECT count(*) FROM {_quote(engine, name)}")).scalar() or 0)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return out
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True)[:limit])
