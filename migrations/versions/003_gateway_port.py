"""gateways: add port column (needed for real communication tests)

Revision ID: 003_gateway_port
Revises: 002_binary_storage
Create Date: 2026-09-18

Idempotent: safe to run against a DB that was created after this column
already existed via Base.metadata.create_all (e.g. a brand-new SQLite
standalone DB).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_gateway_port"
down_revision: Union[str, None] = "002_binary_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "gateways")
    if "port" not in cols:
        op.add_column("gateways", sa.Column("port", sa.Integer()))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "gateways")
    if "port" in cols:
        op.drop_column("gateways", "port")
