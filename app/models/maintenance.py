"""Maintenance, Asset, and Lab domain models (8 tables)."""
from sqlalchemy import JSON, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func

from app.db.base import Base


# ---------- Maintenance (4) ----------
class WorkOrder(Base):
    __tablename__ = "work_orders"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="SET NULL"))
    alarm_id = Column(Integer, ForeignKey("alarms.id", ondelete="SET NULL"))
    title = Column(String(200), nullable=False)
    order_type = Column(String(30), default="repair")
    priority = Column(String(20), default="medium")
    status = Column(String(20), nullable=False, default="open")
    assigned_to = Column(Integer, ForeignKey("users.id"))
    created_by = Column(Integer, ForeignKey("users.id"))
    due_date = Column(Date)
    closed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # 知识库 -> 工单: 报警自动建单时带上 AI 诊断、KB 摘录与建议 SOP
    description = Column(Text)
    diagnosis_id = Column(Integer, ForeignKey("ai_diagnoses.id", ondelete="SET NULL"))
    sop_id = Column(Integer, ForeignKey("maintenance_sop.id", ondelete="SET NULL"))


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"

    id = Column(Integer, primary_key=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id", ondelete="CASCADE"))
    device_id = Column(Integer, ForeignKey("devices.id"))
    action_taken = Column(Text)
    parts_used = Column(Text)
    cost = Column(Numeric(12, 2))
    performed_by = Column(Integer, ForeignKey("users.id"))
    performed_at = Column(DateTime(timezone=True), server_default=func.now())
    # 维修结果: fixed / partially_fixed / not_fixed / no_fault_found + 实际根因
    outcome = Column(String(20))
    root_cause = Column(Text)


class MaintenanceSOP(Base):
    __tablename__ = "maintenance_sop"

    id = Column(Integer, primary_key=True)
    device_model_id = Column(Integer, ForeignKey("device_models.id"))
    sop_type = Column(String(30), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    version = Column(String(20), default="1.0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SparePart(Base):
    __tablename__ = "spare_parts"

    id = Column(Integer, primary_key=True)
    part_code = Column(String(50), unique=True, nullable=False)
    name = Column(String(150), nullable=False)
    device_model_id = Column(Integer, ForeignKey("device_models.id"))
    stock_qty = Column(Integer, nullable=False, default=0)
    unit_price = Column(Numeric(12, 2))
    supplier = Column(String(150))
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------- Asset (2) ----------
class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True)
    asset_code = Column(String(50), unique=True, nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id"))
    name = Column(String(150), nullable=False)
    category = Column(String(50))
    purchase_date = Column(Date)
    purchase_value = Column(Numeric(14, 2))
    depreciation_years = Column(Integer, default=10)
    current_value = Column(Numeric(14, 2))
    status = Column(String(20), default="in_use")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AssetDocument(Base):
    __tablename__ = "asset_documents"

    id = Column(Integer, primary_key=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    doc_name = Column(String(255), nullable=False)
    doc_type = Column(String(30))
    file_path = Column(String(500))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------- Lab (2) ----------
class LabSample(Base):
    __tablename__ = "lab_samples"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"))
    component_name = Column(String(100))
    sample_no = Column(String(50))
    lab_value = Column(Numeric(14, 6))
    online_value = Column(Numeric(14, 6))
    sampled_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(Integer, ForeignKey("users.id"))


class LabCorrelationResult(Base):
    __tablename__ = "lab_correlation_results"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"))
    component_name = Column(String(100))
    sample_count = Column(Integer, nullable=False, default=0)
    r_squared = Column(Numeric(6, 4))
    slope = Column(Numeric(12, 6))
    intercept = Column(Numeric(12, 6))
    mean_deviation = Column(Numeric(12, 6))
    computed_at = Column(DateTime(timezone=True), server_default=func.now())


class LabAICertification(Base):
    """Audit trail of "AI certification" runs: lab samples compared with the analyzer's own
    measurement data (Data Analysis) and judged against explicit criteria. NOT a metrological
    accreditation — see app/services/lab_analysis.py."""

    __tablename__ = "lab_ai_certifications"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)  # certified / conditional / failed / insufficient
    score = Column(Numeric(5, 1))
    window_minutes = Column(Integer, nullable=False, default=30)
    sample_count = Column(Integer, nullable=False, default=0)  # lab samples with a lab value
    pair_count = Column(Integer, nullable=False, default=0)  # samples matched to online data
    r_squared = Column(Numeric(8, 6))
    slope = Column(Numeric(14, 6))
    intercept = Column(Numeric(14, 6))
    bias_pct = Column(Numeric(12, 4))
    mape_pct = Column(Numeric(12, 4))
    criteria = Column(JSON)
    checks = Column(JSON)
    stats = Column(JSON)
    narrative = Column(Text)
    generated_by = Column(String(20))  # llm / template
    certified_at = Column(DateTime(timezone=True), server_default=func.now())
    valid_until = Column(DateTime(timezone=True))

    # (device_id, component_name) is looked up on every AI-selection ranking and on the Lab
    # "Comparison" overview (latest-per-component), so it earns its own index.
    __table_args__ = (Index("ix_lab_ai_certifications_device_component", "device_id", "component_name"),)
