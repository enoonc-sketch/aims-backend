"""measurement_imports: add source + external_database_id (云数据 <-> 数据管理 绑定)

Revision ID: 006_measurement_import_source
Revises: 005_business_indexes
Create Date: 2026-09-28

Every cloud (external data source) sync now records a MeasurementImport batch
and tags the measurements it writes with that batch's import_id, so the
数据管理 page can show / filter history by origin (local upload vs 云数据).

Idempotent: skips columns that already exist (fresh DBs built via create_all).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_measurement_import_source"
down_revision: Union[str, None] = "005_business_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("measurement_imports")}


def upgrade() -> None:
    bind = op.get_bind()
    have = _cols(bind)
    if "source" not in have:
        op.add_column("measurement_imports", sa.Column("source", sa.String(20), nullable=False, server_default="local"))
    if "external_database_id" not in have:
        op.add_column("measurement_imports", sa.Column("external_database_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    have = _cols(bind)
    if "external_database_id" in have:
        op.drop_column("measurement_imports", "external_database_id")
    if "source" in have:
        op.drop_column("measurement_imports", "source")
