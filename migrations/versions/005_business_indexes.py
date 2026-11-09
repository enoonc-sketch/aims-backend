"""devices/measurements/alarms: add plain indexes that used to live only in
the now-deleted db/init/01_schema.sql

Revision ID: 005_business_indexes
Revises: 004_component_tag_comm
Create Date: 2026-09-26

db/init/01_schema.sql duplicated the whole schema by hand alongside the
SQLAlchemy models and had drifted from them (see the 004 migration's columns,
which were missing from that SQL file entirely). That file is now deleted;
ORM models + this migration chain are the single source of truth. These five
indexes existed only in that SQL file and are now declared as `Index(...)`
on the models (Device.status, Measurement.device_id+time,
Measurement.component_name, Alarm.device_id+status, Alarm.triggered_at) —
this migration adds them for any database that was created before this
change and won't otherwise pick them up.

Idempotent: safe to run against a database that already has these indexes
(e.g. a brand-new DB created after this change via Base.metadata.create_all).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_business_indexes"
down_revision: Union[str, None] = "004_component_tag_comm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = [
    ("idx_devices_status", "devices", ["status"]),
    ("idx_measurements_device_time", "measurements", ["device_id", "time"]),
    ("idx_measurements_component", "measurements", ["component_name"]),
    ("idx_alarms_device_status", "alarms", ["device_id", "status"]),
    ("idx_alarms_triggered_at", "alarms", ["triggered_at"]),
]


def _existing_index_names(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    try:
        return {ix["name"] for ix in insp.get_indexes(table)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, columns in _INDEXES:
        if name in _existing_index_names(bind, table):
            continue
        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _columns in _INDEXES:
        if name in _existing_index_names(bind, table):
            op.drop_index(name, table_name=table)
