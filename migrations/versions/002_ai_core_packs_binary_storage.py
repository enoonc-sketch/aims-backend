"""ai_core_packs: add storage/file_path/sha256 columns for real (binary) model packs

Revision ID: 002_binary_storage
Revises: 001_initial
Create Date: 2026-09-18

Lets ai_core_packs track large binary files (LLM / embedding GGUF weights)
stored on disk instead of inline JSON/text in the database. Idempotent: safe
to run against a DB that was created after this column already existed via
Base.metadata.create_all (e.g. a brand-new SQLite standalone DB).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_binary_storage"
down_revision: Union[str, None] = "001_initial"
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
    cols = _existing_columns(bind, "ai_core_packs")
    if "storage" not in cols:
        op.add_column("ai_core_packs", sa.Column("storage", sa.String(10), server_default="db"))
    if "file_path" not in cols:
        op.add_column("ai_core_packs", sa.Column("file_path", sa.String(500)))
    if "sha256" not in cols:
        op.add_column("ai_core_packs", sa.Column("sha256", sa.String(64)))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "ai_core_packs")
    with op.batch_alter_table("ai_core_packs") as batch:
        if "sha256" in cols:
            batch.drop_column("sha256")
        if "file_path" in cols:
            batch.drop_column("file_path")
        if "storage" in cols:
            batch.drop_column("storage")
