"""Database initialization: create all tables + optional demo seed data.

Works for both PostgreSQL (Docker) and SQLite (standalone EXE).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from random import random

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_engine, get_session_factory

# Import all models so Base.metadata is complete
import app.models  # noqa: F401

logger = logging.getLogger("aims.init_db")


def _ensure_sqlite_knowledge_fts(engine) -> None:
    """Create the FTS5 virtual table + sync triggers used for offline keyword
    search over knowledge_chunks on SQLite (no pgvector / pg_trgm available),
    and backfill any rows that existed before this table was introduced.

    Safe to call repeatedly; swallows errors so a build of SQLite without the
    FTS5 module compiled in can't take down startup (rag.py falls back to a
    full bag-of-words scan in that case).
    """
    try:
        with engine.begin() as conn:
            # tokenize='trigram': the default unicode61 tokenizer never splits
            # CJK text into words (a whole run of Chinese characters is one
            # token), so it can't do useful keyword search on Chinese content.
            # trigram indexes overlapping 3-character windows instead, which
            # supports substring search for Chinese and still works for
            # English/numbers (see _fts_match_expr in app/services/rag.py).
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5("
                "content, content='knowledge_chunks', content_rowid='id', tokenize='trigram')"
            ))
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ai AFTER INSERT ON knowledge_chunks BEGIN "
                "INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.id, new.content); END"
            ))
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ad AFTER DELETE ON knowledge_chunks BEGIN "
                "INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content); END"
            ))
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS knowledge_chunks_au AFTER UPDATE ON knowledge_chunks BEGIN "
                "INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content); "
                "INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.id, new.content); END"
            ))
            # Backfill rows inserted before the FTS table existed.
            conn.execute(text(
                "INSERT INTO knowledge_chunks_fts(rowid, content) "
                "SELECT id, content FROM knowledge_chunks "
                "WHERE id NOT IN (SELECT rowid FROM knowledge_chunks_fts)"
            ))
        logger.info("Knowledge base FTS5 index ready (SQLite).")
    except Exception:
        logger.warning("Could not set up FTS5 for knowledge_chunks — falling back to bag-of-words search.", exc_info=True)


def _ensure_postgres_extras(engine) -> None:
    """Postgres/TimescaleDB-only setup that plain `Base.metadata.create_all()`
    cannot express: converting `measurements` into a TimescaleDB hypertable,
    and the pgvector ivfflat similarity index on `knowledge_chunks.embedding`.

    This used to live in a hand-maintained `db/init/01_schema.sql`, duplicating
    every table/column already declared on the ORM models — the two drifted
    (device_components.gateway_id/tag_address existed on the model but not in
    that SQL file). ORM models + Alembic migrations are now the single source
    of truth for all table/column/plain-index DDL on every backend; this
    function only adds the handful of things that are genuinely Postgres-only
    and have no portable SQLAlchemy equivalent, and it runs *after*
    create_all() so the tables it operates on already exist.

    Safe to call repeatedly (idempotent), and swallows errors the same way
    `_ensure_sqlite_knowledge_fts` does, so an environment without TimescaleDB
    or pgvector installed can't take down startup — it just runs as plain
    Postgres tables instead.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT create_hypertable('measurements', 'time', if_not_exists => TRUE)"))
    except Exception:
        logger.warning("Could not convert 'measurements' into a TimescaleDB hypertable (extension missing?).", exc_info=True)

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding "
                "ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            ))
    except Exception:
        logger.warning("Could not create pgvector ivfflat index on knowledge_chunks.embedding (pgvector missing?).", exc_info=True)


def _ensure_sqlite_measurement_import_columns(engine) -> None:
    """SQLite has no Alembic in the standalone EXE: create_all() never adds columns to a
    table that already exists, so add measurement_imports.source / external_database_id
    (云数据 <-> 数据管理 绑定) to databases created before those columns existed."""
    try:
        with engine.begin() as conn:
            have = {r[1] for r in conn.execute(text("PRAGMA table_info(measurement_imports)")).fetchall()}
            if have and "source" not in have:
                conn.execute(text("ALTER TABLE measurement_imports ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'local'"))
            if have and "external_database_id" not in have:
                conn.execute(text("ALTER TABLE measurement_imports ADD COLUMN external_database_id INTEGER"))
    except Exception:
        logger.warning("Could not add source columns to measurement_imports.", exc_info=True)


# 闭环(报警->AI诊断->工单->维修结果->AI反馈)新增的列。SQLite(独立 EXE)没有 Alembic,
# create_all() 不会给已存在的表加列, 所以对旧库在这里补。新表(ai_diagnosis_feedback)由 create_all 建。
_SQLITE_CHAIN_COLUMNS: dict[str, dict[str, str]] = {
    "alarms": {"alarm_type": "VARCHAR(20) NOT NULL DEFAULT 'process'"},
    "ai_diagnoses": {
        "alarm_id": "INTEGER REFERENCES alarms(id) ON DELETE SET NULL",
        "source": "VARCHAR(20) DEFAULT 'manual'",
        "evidence": "JSON",
    },
    "work_orders": {
        "description": "TEXT",
        "diagnosis_id": "INTEGER REFERENCES ai_diagnoses(id) ON DELETE SET NULL",
        "sop_id": "INTEGER REFERENCES maintenance_sop(id) ON DELETE SET NULL",
    },
    "maintenance_records": {"outcome": "VARCHAR(20)", "root_cause": "TEXT"},
    # 真实本地模型(GGUF)支持新增的列。旧 SQLite 库缺这三列时, 任何 db.query(AICorePack)
    # 都会报 "no such column: ai_core_packs.storage", 表现为模型列表/切换/聊天全部不可用。
    "ai_core_packs": {
        "storage": "VARCHAR(10) DEFAULT 'db'",
        "file_path": "VARCHAR(500)",
        "sha256": "VARCHAR(64)",
    },
}


def _ensure_sqlite_chain_columns(engine) -> None:
    for table, cols in _SQLITE_CHAIN_COLUMNS.items():
        try:
            with engine.begin() as conn:
                have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
                if not have:
                    continue
                for name, ddl in cols.items():
                    if name not in have:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        except Exception:
            logger.warning("Could not add chain columns to %s.", table, exc_info=True)


def create_tables() -> None:
    """Create all tables defined in ORM models (idempotent)."""
    logger.info("Creating database tables (if not exist)...")
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    if settings.is_sqlite:
        _ensure_sqlite_measurement_import_columns(engine)
        _ensure_sqlite_chain_columns(engine)
        _ensure_sqlite_knowledge_fts(engine)
    else:
        _ensure_postgres_extras(engine)
    logger.info("Tables ready.")


def _seed_roles(db: Session) -> None:
    """Idempotently create the built-in roles (no users, no credentials)."""
    from app.models.system import Role

    for code, name, desc in (
        ("admin", "Administrator", "Full system access"),
        ("engineer", "Engineer", "Device & maintenance operations"),
        ("viewer", "Viewer", "Read-only access"),
    ):
        if db.scalar(select(Role).where(Role.code == code)) is None:
            db.add(Role(code=code, name=name, description=desc))
    db.flush()


# Alias: secure 包的 manage_users.py 从 app.db.init_db 导入 _ensure_roles;
# 它与 _seed_roles 是同一份角色种子(secure 版末尾多一次 commit, 这里用 flush
# 即可, 调用方随后会 commit)。只为兼容该引用, 其余代码请用 _seed_roles。
_ensure_roles = _seed_roles


def _bootstrap_admin(db: Session) -> None:
    """Create the FIRST administrator, only if the users table is empty AND the
    credentials were injected from outside (BOOTSTRAP_ADMIN_USERNAME / _PASSWORD).

    There is no built-in account and no default password. Without injected
    credentials nobody can log in until an operator runs
    `python -m app.cli create-admin`."""
    from app.core.security import hash_password, validate_password_strength
    from app.models.system import Role, User, UserRole

    if db.execute(select(User).limit(1)).scalar_one_or_none() is not None:
        return
    username = settings.BOOTSTRAP_ADMIN_USERNAME.strip()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    dev_admin = False
    if not username or not password:
        if settings.dev_admin_allowed:
            # Development convenience: well-known account, refused in production
            # by config validation. The password policy is intentionally skipped.
            username = settings.DEV_ADMIN_USERNAME.strip() or "admin"
            password = settings.DEV_ADMIN_PASSWORD
            dev_admin = True
        else:
            logger.warning(
                "No users exist and no BOOTSTRAP_ADMIN_USERNAME/BOOTSTRAP_ADMIN_PASSWORD were provided. "
                "Create the first administrator with:  python -m app.cli create-admin"
            )
            return
    if not dev_admin:
        try:
            validate_password_strength(password, username)
        except ValueError as e:
            raise RuntimeError(f"BOOTSTRAP_ADMIN_PASSWORD rejected: {e}") from e

    admin = User(username=username, hashed_password=hash_password(password),
                 full_name="System Administrator", is_superuser=True)
    db.add(admin)
    db.flush()
    role = db.scalar(select(Role).where(Role.code == "admin"))
    if role is not None:
        db.add(UserRole(user_id=admin.id, role_id=role.id))
    db.commit()
    if dev_admin:
        logger.warning("DEVELOPMENT administrator %r created with the default dev password. "
                       "Never expose this instance; production refuses this account.", username)
    else:
        logger.info("Bootstrap administrator %r created from injected credentials. "
                    "Remove BOOTSTRAP_ADMIN_PASSWORD from the environment now.", username)


def _seed_demo_data_if_empty(db: Session) -> None:
    """Optional demo devices/alarms/etc. Never runs in production and never creates
    user accounts. Enabled only with SEED_DEMO_DATA=true."""
    from app.models.system import User
    from app.models.device import DeviceModel, Device, DeviceComponent, Gateway, GatewayDevice
    from app.models.measurement import Measurement
    from app.models.alarm import AlarmRule, Alarm
    from app.models.maintenance import MaintenanceSOP
    from app.models.analysis import DashboardSnapshot

    if db.execute(select(Device).limit(1)).scalar_one_or_none() is not None:
        logger.info("Devices already exist — skip demo seeding.")
        return

    logger.info("Seeding demo data...")
    # demo rows that record an author use the first superuser, if one exists (else NULL)
    owner_id = db.scalar(select(User.id).where(User.is_superuser.is_(True)).order_by(User.id).limit(1))

    # Device models
    models = [
        DeviceModel(model_name="GC-2000", manufacturer="AnalyzerCo", analyzer_type="GC",
                    description="Process gas chromatograph"),
        DeviceModel(model_name="FTIR-500", manufacturer="AnalyzerCo", analyzer_type="FTIR",
                    description="Fourier-transform infrared analyzer"),
    ]
    db.add_all(models)
    db.flush()

    # Devices
    devices = [
        Device(
            device_code="Analyzer-001",
            name="Process Gas Analyzer 001",
            model_id=models[0].id,
            protocol="Modbus TCP",
            location="Unit 1 - Rack A",
            status="online",
            health_score=96.5,
            ip_address="10.0.1.11",
            installed_at=datetime(2023, 5, 10).date(),
        ),
        Device(
            device_code="Analyzer-002",
            name="Process Gas Analyzer 002",
            model_id=models[1].id,
            protocol="OPC UA",
            location="Unit 2 - Rack B",
            status="warning",
            health_score=82.0,
            ip_address="10.0.1.12",
            installed_at=datetime(2023, 8, 22).date(),
        ),
    ]
    db.add_all(devices)
    db.flush()

    # Components
    component_names = [
        "Nitrogen", "Carbon Monoxide", "Carbon Dioxide", "Methane", "Ethylene",
        "Propylene", "All Butenes", "All Pentenes", "1,3-Butadiene", "Hydrogen Sulfide",
        "Ethane", "Propane", "Isobutane", "n-Butane", "Isopentane",
        "n-Pentane", "Hexane+", "Hydrogen",
    ]
    for d in devices:
        for name in component_names:
            db.add(DeviceComponent(
                device_id=d.id,
                component_name=name,
                unit="%",
                is_major=True,
                low_limit=0,
                high_limit=100,
            ))

    # Gateway
    gw = Gateway(
        gateway_code="GW-001",
        name="Field Gateway 001",
        ip_address="10.0.1.1",
        protocol="MQTT",
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
    )
    db.add(gw)
    db.flush()
    for d in devices:
        db.add(GatewayDevice(gateway_id=gw.id, device_id=d.id))

    # Sample measurements (last 24h, hourly) for Analyzer-001
    now = datetime.now(timezone.utc)
    d0 = devices[0]
    for n in range(24):
        t = now - timedelta(hours=n)
        db.add(Measurement(
            time=t,
            device_id=d0.id,
            component_name="Nitrogen",
            value=40 + random() * 5,
            quality="good",
        ))
        db.add(Measurement(
            time=t,
            device_id=d0.id,
            component_name="Methane",
            value=25 + random() * 3,
            quality="good",
        ))

    # Alarm rule + one active alarm on Analyzer-002
    d1 = devices[1]
    rule = AlarmRule(
        device_id=d1.id,
        component_name="Methane",
        rule_type="threshold",
        low_limit=20,
        high_limit=30,
        severity="warning",
    )
    db.add(rule)
    db.flush()
    db.add(Alarm(
        device_id=d1.id,
        rule_id=rule.id,
        component_name="Methane",
        severity="warning",
        message="Methane concentration approaching high limit",
        value=29.4,
        status="active",
    ))

    # Maintenance SOPs
    db.add(MaintenanceSOP(
        device_model_id=models[0].id,
        sop_type="Calibration",
        title="GC-2000 Calibration Procedure",
        content="Step 1: Isolate sample line...\nStep 2: Inject calibration gas...\nStep 3: Verify component sum 99-104%...",
        version="1.0",
    ))
    db.add(MaintenanceSOP(
        device_model_id=models[1].id,
        sop_type="Maintenance",
        title="FTIR-500 Routine Maintenance",
        content="Step 1: Check purge gas pressure...\nStep 2: Clean optical windows...",
        version="1.0",
    ))


    # Spare parts
    from app.models.maintenance import SparePart, WorkOrder, Asset
    db.add_all([
        SparePart(part_code="SP-GC-COL", name="GC Column GC-2000", device_model_id=models[0].id,
                  stock_qty=5, unit_price=1200.0, supplier="AnalyzerCo"),
        SparePart(part_code="SP-FTIR-CELL", name="FTIR Sample Cell", device_model_id=models[1].id,
                  stock_qty=3, unit_price=850.0, supplier="AnalyzerCo"),
        SparePart(part_code="SP-FILTER", name="Sample Filter Element", device_model_id=None,
                  stock_qty=20, unit_price=45.0, supplier="Generic"),
    ])

    # Assets linked to devices
    db.add_all([
        Asset(asset_code="AST-001", device_id=devices[0].id, name="Process Gas Analyzer 001",
              category="Analyzer", purchase_date=datetime(2023, 5, 10).date(),
              purchase_value=280000, depreciation_years=10, current_value=196000, status="in_use"),
        Asset(asset_code="AST-002", device_id=devices[1].id, name="Process Gas Analyzer 002",
              category="Analyzer", purchase_date=datetime(2023, 8, 22).date(),
              purchase_value=320000, depreciation_years=10, current_value=256000, status="in_use"),
    ])

    # One open work order
    db.add(WorkOrder(
        device_id=devices[1].id, title="Investigate Methane high reading",
        order_type="repair", priority="high", status="open", created_by=owner_id,
    ))

    # Knowledge base demo
    from app.models.knowledge import KnowledgeDocument, KnowledgeChunk
    kdoc = KnowledgeDocument(
        title="GC-2000 Troubleshooting Guide",
        doc_type="manual",
        device_model_id=models[0].id,
        uploaded_by=owner_id,
        status="ready",
    )
    db.add(kdoc)
    db.flush()
    db.add_all([
        KnowledgeChunk(document_id=kdoc.id, chunk_index=0,
            content="If Methane reading is high, check sample line leak and calibration gas purity."),
        KnowledgeChunk(document_id=kdoc.id, chunk_index=1,
            content="GC-2000 column replacement procedure: cool down, isolate carrier gas, replace column, leak check."),
        KnowledgeChunk(document_id=kdoc.id, chunk_index=2,
            content="Common alarm: Flame Ionization Detector flame out — verify hydrogen and air supply pressures."),
    ])

    # Lab samples for correlation demo
    from app.models.maintenance import LabSample
    for i, (lab_v, online_v) in enumerate([(25.1, 25.4), (26.0, 26.3), (24.5, 24.8), (27.2, 27.0)]):
        db.add(LabSample(
            device_id=devices[0].id, component_name="Methane",
            sample_no=f"LAB-2024-{i+1:03d}", lab_value=lab_v, online_value=online_v,
            created_by=owner_id,
        ))

    # System settings
    from app.models.system import SystemSetting
    db.add_all([
        SystemSetting(key="site_name", value="AIMS Demo Plant", description="Display name"),
        SystemSetting(key="data_retention_days", value="365", description="Measurement retention"),
        SystemSetting(key="alarm_email_enabled", value="false", description="Send alarm emails"),
    ])


    # Dashboard snapshot
    db.add(DashboardSnapshot(
        total_devices=2,
        online_devices=1,
        alarms_today=1,
        faults=0,
        open_work_orders=1,
    ))

    _seed_permissions(db)
    db.commit()
    logger.info("Demo data seeded (no accounts were created).")


def _ensure_indexes(engine) -> None:
    """Indexes added after the first release; CREATE INDEX IF NOT EXISTS is portable
    (SQLite + PostgreSQL) and create_all() never adds indexes to existing tables."""
    stmts = (
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_user_action ON audit_logs (user_id, action)",
    )
    try:
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))
    except Exception:
        logger.warning("Could not create audit_logs indexes.", exc_info=True)


_LEGACY_DEMO_CREDENTIALS = (
    ("admin", "admin123"), ("system", "sys123"), ("operator", "op123"), ("maintainer", "maint123"),
)


def _disable_legacy_demo_accounts(db: Session) -> int:
    """Databases created by older builds still contain the seeded demo accounts.
    Any account that still authenticates with its well-known demo password is
    deactivated, its sessions are revoked and the event is audited. The operator
    must set a real password (`python -m app.cli reset-password`) and re-enable it."""
    from datetime import datetime, timezone

    from app.core.security import verify_password
    from app.models.system import User
    from app.services.audit import record_audit
    from app.services.auth_sessions import revoke_all_for_user

    disabled = 0
    for username, password in _LEGACY_DEMO_CREDENTIALS:
        if (settings.dev_admin_allowed and username == (settings.DEV_ADMIN_USERNAME.strip() or "admin")
                and password == settings.DEV_ADMIN_PASSWORD):
            continue  # the explicitly allowed development admin keeps working
        user = db.scalar(select(User).where(User.username == username))
        if user is not None and verify_password(password, user.hashed_password):
            user.is_active = False
            revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
            record_audit(db, user_id=None, action="SECURITY_DEFAULT_ACCOUNT_DISABLED", entity="users",
                         entity_id=user.id, detail={"username": username})
            logger.error("Account %r still used its default demo password: it has been DISABLED. "
                         "Run `python -m app.cli reset-password --username %s` to recover it.", username, username)
            disabled += 1
    if disabled:
        db.commit()
    return disabled


def init_db(seed: bool = True) -> None:
    """Full initialization: create tables, built-in roles/permissions and — only when
    credentials were injected — the first administrator.

    `seed=True` additionally loads demo devices *iff* SEED_DEMO_DATA=true and the
    environment is not production. No account is ever created implicitly."""
    create_tables()
    _ensure_indexes(get_engine())
    db = get_session_factory()()
    try:
        _seed_roles(db)
        _seed_permissions(db)
        db.commit()
        _disable_legacy_demo_accounts(db)
        _bootstrap_admin(db)
        if seed and settings.SEED_DEMO_DATA and not settings.is_production:
            _seed_demo_data_if_empty(db)
    except RuntimeError:
        raise  # e.g. weak bootstrap password: must abort startup
    except Exception:
        logger.exception("Database seeding failed")
        db.rollback()
    finally:
        db.close()


def _seed_permissions(db: Session) -> None:
    """Idempotently seed fine-grained permissions used by the industrial APIs."""
    from app.models.system import Permission, Role, RolePermission
    items = [
        ("device.read","Device read","device"),("device.write","Device write","device"),
        ("tag.read","TAG read","tag"),("tag.write","TAG write","tag"),
        ("communication.read","Communication read","communication"),("communication.write","Communication write","communication"),
        ("measurement.read","Measurement read","measurement"),("measurement.write","Measurement write","measurement"),("measurement.import","Measurement import","measurement"),
        ("alarm.ack","Acknowledge alarm","alarm"),("alarm.close","Close alarm","alarm"),
        ("maintenance.create","Create work order","maintenance"),("maintenance.close","Close work order","maintenance"),
        ("knowledge.write","Knowledge write","knowledge"),("ai.run","Run AI","ai"),("ai.admin","AI administration","ai"),("ai.feedback","AI feedback","ai"),
    ]
    for code,name,module in items:
        p=db.scalar(select(Permission).where(Permission.code==code))
        if p is None: db.add(Permission(code=code,name=name,module=module))
    db.flush()
    admin=db.scalar(select(Role).where(Role.code=="admin"))
    if admin:
        for p in db.scalars(select(Permission)).all():
            exists=db.execute(select(RolePermission).where(RolePermission.role_id==admin.id,RolePermission.permission_id==p.id)).first()
            if not exists: db.add(RolePermission(role_id=admin.id,permission_id=p.id))
