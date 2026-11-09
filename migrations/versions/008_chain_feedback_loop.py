"""闭环: 报警 -> AI诊断 -> 知识库 -> 工单 -> 维修结果 -> AI反馈

Revision ID: 008_chain_feedback_loop
Revises: 007_industrial_core
Create Date: 2026-09-29

- alarms.alarm_type                      process / data_quality / comm
- ai_diagnoses.alarm_id/source/evidence  诊断绑定报警, 并落库诊断依据(知识库摘录 / SOP / 证据统计)
- work_orders.description/diagnosis_id/sop_id
- maintenance_records.outcome/root_cause 维修结果
- ai_diagnosis_feedback (新表)           诊断复盘: 工程师评价 + 实际根因, 用于置信度校准与案例回写知识库

Idempotent: 已存在的列/表(全新库由 create_all 建好)会跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008_chain_feedback_loop"
down_revision: Union[str, None] = "007_industrial_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)} if insp.has_table(table) else set()


def _add(bind, table: str, column: sa.Column) -> None:
    have = _cols(bind, table)
    if have and column.name not in have:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add(bind, "alarms", sa.Column("alarm_type", sa.String(20), nullable=False, server_default="process"))

    _add(bind, "ai_diagnoses", sa.Column("alarm_id", sa.Integer(), sa.ForeignKey("alarms.id", ondelete="SET NULL")))
    _add(bind, "ai_diagnoses", sa.Column("source", sa.String(20), server_default="manual"))
    _add(bind, "ai_diagnoses", sa.Column("evidence", sa.JSON()))

    _add(bind, "work_orders", sa.Column("description", sa.Text()))
    _add(bind, "work_orders", sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("ai_diagnoses.id", ondelete="SET NULL")))
    _add(bind, "work_orders", sa.Column("sop_id", sa.Integer(), sa.ForeignKey("maintenance_sop.id", ondelete="SET NULL")))

    _add(bind, "maintenance_records", sa.Column("outcome", sa.String(20)))
    _add(bind, "maintenance_records", sa.Column("root_cause", sa.Text()))

    if not sa.inspect(bind).has_table("ai_diagnosis_feedback"):
        op.create_table(
            "ai_diagnosis_feedback",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("ai_diagnoses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("work_order_id", sa.Integer(), sa.ForeignKey("work_orders.id", ondelete="SET NULL")),
            sa.Column("maintenance_record_id", sa.Integer(), sa.ForeignKey("maintenance_records.id", ondelete="SET NULL")),
            sa.Column("verdict", sa.String(20), nullable=False, server_default="unrated"),
            sa.Column("outcome", sa.String(20)),
            sa.Column("actual_root_cause", sa.Text()),
            sa.Column("actual_action", sa.Text()),
            sa.Column("knowledge_document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("rated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_ai_diagnosis_feedback_diagnosis", "ai_diagnosis_feedback", ["diagnosis_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("ai_diagnosis_feedback"):
        op.drop_index("ix_ai_diagnosis_feedback_diagnosis", table_name="ai_diagnosis_feedback")
        op.drop_table("ai_diagnosis_feedback")
    for table, cols in (
        ("maintenance_records", ("root_cause", "outcome")),
        ("work_orders", ("sop_id", "diagnosis_id", "description")),
        ("ai_diagnoses", ("evidence", "source", "alarm_id")),
        ("alarms", ("alarm_type",)),
    ):
        have = _cols(bind, table)
        for c in cols:
            if c in have:
                op.drop_column(table, c)
