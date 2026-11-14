"""users.must_change_password：默认口令账号首次登录必须先改密码
Revision ID: 010_must_change_password
"""
from alembic import op
import sqlalchemy as sa

revision = "010_must_change_password"
down_revision = "009_security_hardening"
branch_labels = None
depends_on = None


def _has_column():
    insp = sa.inspect(op.get_bind())
    return insp.has_table("users") and any(c["name"] == "must_change_password" for c in insp.get_columns("users"))


def upgrade():
    # 幂等：001_initial 按当前模型 create_all，全新库已带这一列。
    if not _has_column():
        op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))


def downgrade():
    if _has_column():
        op.drop_column("users", "must_change_password")
