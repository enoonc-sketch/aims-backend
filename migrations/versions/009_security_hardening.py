"""security hardening: refresh tokens, login throttling, audit indexes
Revision ID: 009_security_hardening
"""
from alembic import op
import sqlalchemy as sa

revision = "009_security_hardening"
down_revision = "008_chain_feedback_loop"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
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
    op.create_index("idx_refresh_tokens_user", "refresh_tokens", ["user_id"])
    op.create_index("idx_refresh_tokens_family", "refresh_tokens", ["family_id"])

    op.create_table(
        "login_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_login_failures_user_time", "login_failures", ["username", "created_at"])
    op.create_index("idx_login_failures_ip_time", "login_failures", ["ip_address", "created_at"])

    op.create_index("idx_audit_created", "audit_logs", ["created_at"])
    op.create_index("idx_audit_user_action", "audit_logs", ["user_id", "action"])


def downgrade():
    op.drop_index("idx_audit_user_action", table_name="audit_logs")
    op.drop_index("idx_audit_created", table_name="audit_logs")
    op.drop_table("login_failures")
    op.drop_table("refresh_tokens")
