"""AI Center domain models (5 tables): diagnosis, prediction, health, chat."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, func

from app.db.base import Base


class AIDiagnosis(Base):
    __tablename__ = "ai_diagnoses"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100))
    symptom = Column(String(50))
    diagnosis = Column(Text)
    confidence = Column(Numeric(5, 2))
    recommendation = Column(Text)
    requested_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- 报警 -> AI诊断 -> 知识库 -> 工单 闭环 ---
    alarm_id = Column(Integer, ForeignKey("alarms.id", ondelete="SET NULL"))
    # manual = /ai/diagnose 手动; alarm_auto = 报警产生时自动; alarm_manual = /alarms/{id}/diagnose 重跑
    source = Column(String(20), default="manual")
    # 诊断依据快照: {"knowledge": [KB 摘录], "sop": {...}, "evidence": {...}, "feedback_note": ...}
    evidence = Column(JSON)


class AIDiagnosisFeedback(Base):
    """维修结果 -> AI反馈: 一次诊断 + 一张工单的复盘。verdict 由工程师给出(不由系统猜);
    未评价时为 unrated。已评价的记录会校准之后同类诊断的置信度(见 services/repair_feedback.py),
    修复成功的案例还会写回知识库(knowledge_document_id)。"""

    __tablename__ = "ai_diagnosis_feedback"

    id = Column(Integer, primary_key=True)
    diagnosis_id = Column(Integer, ForeignKey("ai_diagnoses.id", ondelete="CASCADE"), nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id", ondelete="SET NULL"))
    maintenance_record_id = Column(Integer, ForeignKey("maintenance_records.id", ondelete="SET NULL"))
    verdict = Column(String(20), nullable=False, default="unrated")  # correct/partially_correct/incorrect/unrated
    outcome = Column(String(20))  # fixed/partially_fixed/not_fixed/no_fault_found
    actual_root_cause = Column(Text)
    actual_action = Column(Text)
    knowledge_document_id = Column(Integer, ForeignKey("knowledge_documents.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    rated_at = Column(DateTime(timezone=True))

    __table_args__ = (Index("ix_ai_diagnosis_feedback_diagnosis", "diagnosis_id"),)


class AIPrediction(Base):
    __tablename__ = "ai_predictions"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100))
    horizon_hours = Column(Integer, nullable=False, default=24)
    predicted_values = Column(JSON)
    model_name = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AIHealthScore(Base):
    __tablename__ = "ai_health_scores"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    score = Column(Numeric(5, 2), nullable=False)
    factors = Column(JSON)
    computed_at = Column(DateTime(timezone=True), server_default=func.now())


class AIChatSession(Base):
    __tablename__ = "ai_chat_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AIChatMessage(Base):
    __tablename__ = "ai_chat_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("ai_chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    sources = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AICorePack(Base):
    """Open-source AI / Data-Analysis core packs, downloaded from cores/manifest.json
    and persisted into the database (in addition to the on-disk cores/vendor/ copy),
    so the frontend can list/serve "downloaded models" straight from the DB.
    """

    __tablename__ = "ai_core_packs"

    id = Column(Integer, primary_key=True)
    core_id = Column(String(100), nullable=False, unique=True)  # manifest "id", e.g. ai-diagnosis-rules
    name = Column(String(200))
    kind = Column(String(20))  # "ai" | "data"
    license = Column(String(50))
    homepage = Column(String(500))
    filename = Column(String(200))
    purpose = Column(Text)
    source_url = Column(String(500))
    content_json = Column(JSON)  # populated when the pack is a JSON rule/weight/prompt pack
    content_text = Column(Text)  # populated when the pack is a JS library / other text asset
    size_bytes = Column(Integer)
    downloaded_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # --- added for real binary model support (LLM / embedding GGUF files) ---
    # "db"  -> content_json/content_text hold the payload (small JSON/JS packs, as before)
    # "disk" -> the payload is a large binary file streamed to disk; only metadata lives here
    storage = Column(String(10), default="db")
    file_path = Column(String(500))  # absolute path on disk, only set when storage == "disk"
    sha256 = Column(String(64))
