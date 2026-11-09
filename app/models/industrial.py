"""Industrial acquisition, TAG, data-quality and AI feedback models."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, func
from app.db.base import Base

class CommunicationConnection(Base):
    __tablename__ = "communication_connections"
    id = Column(Integer, primary_key=True)
    gateway_id = Column(Integer, ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(120), nullable=False)
    protocol = Column(String(40), nullable=False)
    endpoint = Column(String(255))
    port = Column(Integer)
    enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="unknown")
    config = Column(JSON)
    last_error = Column(Text)
    last_connected_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_comm_conn_gateway", "gateway_id"),)

class DeviceTag(Base):
    __tablename__ = "device_tags"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    tag_code = Column(String(100), nullable=False)
    name = Column(String(150), nullable=False)
    data_type = Column(String(30), nullable=False, default="float")
    unit = Column(String(30))
    description = Column(Text)
    min_value = Column(Numeric(18, 6))
    max_value = Column(Numeric(18, 6))
    enabled = Column(Boolean, nullable=False, default=True)
    metadata_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_device_tags_device_code", "device_id", "tag_code", unique=True),)

class TagMapping(Base):
    __tablename__ = "tag_mappings"
    id = Column(Integer, primary_key=True)
    tag_id = Column(Integer, ForeignKey("device_tags.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(Integer, ForeignKey("communication_connections.id", ondelete="SET NULL"))
    address = Column(String(255), nullable=False)
    register_type = Column(String(50))
    scale = Column(Numeric(18, 8), nullable=False, default=1)
    offset = Column(Numeric(18, 8), nullable=False, default=0)
    byte_order = Column(String(20))
    poll_interval_ms = Column(Integer, nullable=False, default=1000)
    enabled = Column(Boolean, nullable=False, default=True)
    last_value = Column(Numeric(18, 8))
    last_timestamp = Column(DateTime(timezone=True))
    __table_args__ = (Index("idx_tag_mapping_address", "connection_id", "address"),)

class AcquisitionEvent(Base):
    __tablename__ = "acquisition_events"
    id = Column(Integer, primary_key=True)
    tag_id = Column(Integer, ForeignKey("device_tags.id", ondelete="SET NULL"))
    connection_id = Column(Integer, ForeignKey("communication_connections.id", ondelete="SET NULL"))
    event_time = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())
    raw_value = Column(String(255))
    engineering_value = Column(Numeric(20, 8))
    quality_code = Column(String(30), nullable=False, default="good")
    source_timestamp = Column(DateTime(timezone=True))
    sequence_number = Column(Integer)
    payload = Column(JSON)
    error = Column(Text)
    __table_args__ = (Index("idx_acq_event_tag_time", "tag_id", "event_time"),)

class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"
    id = Column(Integer, primary_key=True)
    measurement_time = Column(DateTime(timezone=True), nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100), nullable=False)
    quality_code = Column(String(30), nullable=False)
    rule_code = Column(String(60), nullable=False)
    severity = Column(String(20), nullable=False, default="warning")
    details = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_quality_device_time", "device_id", "measurement_time"),)

class ImportValidation(Base):
    __tablename__ = "import_validations"
    id = Column(Integer, primary_key=True)
    import_id = Column(Integer, ForeignKey("measurement_imports.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    summary = Column(JSON, nullable=False, default=dict)
    mapping = Column(JSON)
    issues = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AIModelVersion(Base):
    __tablename__ = "ai_model_versions"
    id = Column(Integer, primary_key=True)
    model_key = Column(String(100), nullable=False)
    version = Column(String(50), nullable=False)
    task = Column(String(60), nullable=False)
    artifact_path = Column(String(500))
    sha256 = Column(String(64))
    metrics = Column(JSON)
    status = Column(String(20), nullable=False, default="registered")
    deployed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_ai_model_version", "model_key", "version", unique=True),)

class AIFeedback(Base):
    __tablename__ = "ai_feedback"
    id = Column(Integer, primary_key=True)
    diagnosis_id = Column(Integer, ForeignKey("ai_diagnoses.id", ondelete="SET NULL"))
    work_order_id = Column(Integer, ForeignKey("work_orders.id", ondelete="SET NULL"))
    outcome = Column(String(40), nullable=False)
    root_cause = Column(String(255))
    corrective_action = Column(Text)
    evidence = Column(JSON)
    verified_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
