"""industrial core: TAG, acquisition, quality, import validation and AI feedback
Revision ID: 007_industrial_core
"""
from alembic import op
import sqlalchemy as sa

# 001_initial 用 Base.metadata.create_all 按"当前模型"建表，全新库一步到位就已经包含本迁移的所有表；
# 应用启动时的自动建表也会补齐缺表。所以本迁移必须幂等（和 002-006、008 一样先检查再建），
# 否则任何全新库上 `alembic upgrade head` 都会在这里报 DuplicateTable。
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


revision = "007_industrial_core"
down_revision = "006_measurement_import_source"
branch_labels = None
depends_on = None

def upgrade():
    _create_table("communication_connections", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("gateway_id",sa.Integer(),sa.ForeignKey("gateways.id",ondelete="CASCADE"),nullable=False), sa.Column("name",sa.String(120),nullable=False), sa.Column("protocol",sa.String(40),nullable=False), sa.Column("endpoint",sa.String(255)), sa.Column("port",sa.Integer()), sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()), sa.Column("status",sa.String(20),nullable=False,server_default="unknown"), sa.Column("config",sa.JSON()), sa.Column("last_error",sa.Text()), sa.Column("last_connected_at",sa.DateTime(timezone=True)), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))
    _create_index("idx_comm_conn_gateway","communication_connections",["gateway_id"])
    _create_table("device_tags", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("device_id",sa.Integer(),sa.ForeignKey("devices.id",ondelete="CASCADE"),nullable=False), sa.Column("tag_code",sa.String(100),nullable=False), sa.Column("name",sa.String(150),nullable=False), sa.Column("data_type",sa.String(30),nullable=False,server_default="float"), sa.Column("unit",sa.String(30)), sa.Column("description",sa.Text()), sa.Column("min_value",sa.Numeric(18,6)), sa.Column("max_value",sa.Numeric(18,6)), sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()), sa.Column("metadata_json",sa.JSON()), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))
    _create_index("uq_device_tags_device_code","device_tags",["device_id","tag_code"],unique=True)
    _create_table("tag_mappings", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("tag_id",sa.Integer(),sa.ForeignKey("device_tags.id",ondelete="CASCADE"),nullable=False), sa.Column("connection_id",sa.Integer(),sa.ForeignKey("communication_connections.id",ondelete="SET NULL")), sa.Column("address",sa.String(255),nullable=False), sa.Column("register_type",sa.String(50)), sa.Column("scale",sa.Numeric(18,8),nullable=False,server_default="1"), sa.Column("offset",sa.Numeric(18,8),nullable=False,server_default="0"), sa.Column("byte_order",sa.String(20)), sa.Column("poll_interval_ms",sa.Integer(),nullable=False,server_default="1000"), sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()), sa.Column("last_value",sa.Numeric(18,8)), sa.Column("last_timestamp",sa.DateTime(timezone=True)))
    _create_index("idx_tag_mapping_address","tag_mappings",["connection_id","address"])
    _create_table("acquisition_events", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("tag_id",sa.Integer(),sa.ForeignKey("device_tags.id",ondelete="SET NULL")), sa.Column("connection_id",sa.Integer(),sa.ForeignKey("communication_connections.id",ondelete="SET NULL")), sa.Column("event_time",sa.DateTime(timezone=True),nullable=False), sa.Column("received_at",sa.DateTime(timezone=True),server_default=sa.func.now()), sa.Column("raw_value",sa.String(255)), sa.Column("engineering_value",sa.Numeric(20,8)), sa.Column("quality_code",sa.String(30),nullable=False,server_default="good"), sa.Column("source_timestamp",sa.DateTime(timezone=True)), sa.Column("sequence_number",sa.Integer()), sa.Column("payload",sa.JSON()), sa.Column("error",sa.Text()))
    _create_index("idx_acq_event_tag_time","acquisition_events",["tag_id","event_time"])
    _create_table("data_quality_events", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("measurement_time",sa.DateTime(timezone=True),nullable=False), sa.Column("device_id",sa.Integer(),sa.ForeignKey("devices.id",ondelete="CASCADE"),nullable=False), sa.Column("component_name",sa.String(100),nullable=False), sa.Column("quality_code",sa.String(30),nullable=False), sa.Column("rule_code",sa.String(60),nullable=False), sa.Column("severity",sa.String(20),nullable=False,server_default="warning"), sa.Column("details",sa.JSON()), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))
    _create_index("idx_quality_device_time","data_quality_events",["device_id","measurement_time"])
    _create_table("import_validations", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("import_id",sa.Integer(),sa.ForeignKey("measurement_imports.id",ondelete="CASCADE"),nullable=False), sa.Column("status",sa.String(20),nullable=False,server_default="pending"), sa.Column("summary",sa.JSON(),nullable=False), sa.Column("mapping",sa.JSON()), sa.Column("issues",sa.JSON()), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))
    _create_table("ai_model_versions", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("model_key",sa.String(100),nullable=False), sa.Column("version",sa.String(50),nullable=False), sa.Column("task",sa.String(60),nullable=False), sa.Column("artifact_path",sa.String(500)), sa.Column("sha256",sa.String(64)), sa.Column("metrics",sa.JSON()), sa.Column("status",sa.String(20),nullable=False,server_default="registered"), sa.Column("deployed",sa.Boolean(),nullable=False,server_default=sa.false()), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))
    _create_index("uq_ai_model_version","ai_model_versions",["model_key","version"],unique=True)
    _create_table("ai_feedback", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("diagnosis_id",sa.Integer(),sa.ForeignKey("ai_diagnoses.id",ondelete="SET NULL")), sa.Column("work_order_id",sa.Integer(),sa.ForeignKey("work_orders.id",ondelete="SET NULL")), sa.Column("outcome",sa.String(40),nullable=False), sa.Column("root_cause",sa.String(255)), sa.Column("corrective_action",sa.Text()), sa.Column("evidence",sa.JSON()), sa.Column("verified_by",sa.Integer(),sa.ForeignKey("users.id",ondelete="SET NULL")), sa.Column("verified_at",sa.DateTime(timezone=True)), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now()))

def downgrade():
    for t in ["ai_feedback","ai_model_versions","import_validations","data_quality_events","acquisition_events","tag_mappings","device_tags","communication_connections"]:
        if _has_table(t):
            op.drop_table(t)
