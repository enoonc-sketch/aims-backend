"""device_components: add gateway_id + tag_address (TAG -> communication link)

Revision ID: 004_component_tag_comm
Revises: 003_gateway_port
Create Date: 2026-09-19

Turns DeviceComponent into a first-class TAG: gateway_id + tag_address give it
a real FK path from 设备(Device) -> TAG(DeviceComponent) -> 通讯(Gateway),
instead of components only ever being grouped by device.

Idempotent: safe to run against a DB that was created after these columns
already existed via Base.metadata.create_all (e.g. a brand-new SQLite
standalone DB).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_component_tag_comm"
down_revision: Union[str, None] = "003_gateway_port"
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
    cols = _existing_columns(bind, "device_components")
    if "gateway_id" not in cols:
        op.add_column(
            "device_components",
            sa.Column("gateway_id", sa.Integer(), sa.ForeignKey("gateways.id", ondelete="SET NULL")),
        )
    if "tag_address" not in cols:
        op.add_column("device_components", sa.Column("tag_address", sa.String(length=100)))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "device_components")
    if "tag_address" in cols:
        op.drop_column("device_components", "tag_address")
    if "gateway_id" in cols:
        op.drop_column("device_components", "gateway_id")
