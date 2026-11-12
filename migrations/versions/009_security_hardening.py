"""security hardening: refresh tokens, login throttling, audit indexes
Revision ID: 009_security_hardening
"""
from alembic import op
import sqlalchemy as sa

# 与 007 相同的原因：001_initial 按当前模型 create_all，全新库已包含这些表/索引，
# 应用启动的自动建表也会补齐；迁移必须幂等，否则 `alembic upgrade head` 会报 DuplicateTable。
def _has_table(name):
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table, name):
    insp = sa.inspect(op.get_bind())
    return insp.has_table(table) and any(i["name"] == name for i in insp.get_indexes(table))


def _create_table(name, *cols, **kw):
    if not _has_table(name):
        op.create_table(name, *cols, **kw)


def _create_index(name, table, columns, **kw):
    if not _has_index(table, name):
        op.create_index(name, table, columns, **kw)


def _drop_index(name, table):
    if _has_index(table, name):
        op.drop_index(name, table_name=table)


revision = "009_security_hardening"
down_revision = "008_chain_feedback_loop"
branch_labels = None
depends_on = None


def upgrade():
    _create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(255)),
    )
    _create_index("idx_refresh_tokens_user", "refresh_tokens", ["user_id"])
    _create_index("idx_refresh_tokens_family", "refresh_tokens", ["family_id"])

    _create_table(
        "login_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    _create_index("idx_login_failures_user_time", "login_failures", ["username", "created_at"])
    _create_index("idx_login_failures_ip_time", "login_failures", ["ip_address", "created_at"])

    _create_index("idx_audit_created", "audit_logs", ["created_at"])
    _create_index("idx_audit_user_action", "audit_logs", ["user_id", "action"])


def downgrade():
    _drop_index("idx_audit_user_action", "audit_logs")
    _drop_index("idx_audit_created", "audit_logs")
    for t in ("login_failures", "refresh_tokens"):
        if _has_table(t):
            op.drop_table(t)
