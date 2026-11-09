from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from app.schemas.ai_studio import KnowledgeRef


class DiagnosisRequest(BaseModel):
    device_id: int
    component_name: str
    # Drift / Noise / Response — optional: when omitted (or blank), the symptom is
    # auto-detected from the component's last 24h of measurements (see
    # app/services/symptom_detect.py) so diagnosis works from raw data alone.
    symptom: Optional[str] = None
    # 给某条报警做诊断: 诊断会绑定到该报警, 并刷新它尚未完成的工单
    alarm_id: Optional[int] = None


class DiagnosisResult(BaseModel):
    id: Optional[int] = None
    alarm_id: Optional[int] = None
    device_id: int
    component_name: str
    symptom: str
    symptom_auto_detected: bool = False
    diagnosis: str
    confidence: float
    recommendation: str
    created_at: Optional[datetime] = None
    # Knowledge-base excerpts relevant to this fault (empty when the KB has nothing on it)
    references: list[KnowledgeRef] = []


class PredictionRequest(BaseModel):
    device_id: int
    component_name: str
    horizon_hours: int = 24


class PredictionPoint(BaseModel):
    time: datetime
    value: float
    lower: Optional[float] = None  # 95% confidence lower bound
    upper: Optional[float] = None  # 95% confidence upper bound


class PredictionResult(BaseModel):
    device_id: int
    component_name: str
    horizon_hours: int
    model_name: str
    trend: Optional[str] = None  # "rising" | "falling" | "stable"
    predicted_values: list[PredictionPoint]


class HealthScoreResult(BaseModel):
    device_id: int
    score: float
    factors: dict


class ChatRequest(BaseModel):
    session_id: Optional[int] = None
    message: str
    use_rag: bool = False  # retrieve knowledge base context before answering
    # Optional scope: the device picked in AI Center. When set, the live-data context
    # is built around this device (detail, TAGs, recent readings, alarms, work orders,
    # asset) instead of guessing a device from the question text.
    device_id: Optional[int] = None


class ChatResponse(BaseModel):
    session_id: int
    answer: str
    reply: Optional[str] = None  # alias of answer for frontend convenience
    sources: list[str] = []


class CorePackOut(BaseModel):
    """Status of one open-source core/model pack (manifest entry merged with DB row)."""

    id: str  # manifest core id, e.g. ai-diagnosis-rules
    name: Optional[str] = None
    kind: Optional[str] = None
    license: Optional[str] = None
    homepage: Optional[str] = None
    filename: Optional[str] = None
    purpose: Optional[str] = None
    in_db: bool = False
    size_bytes: Optional[int] = None
    downloaded_at: Optional[datetime] = None
    engine: Optional[str] = None
    params: Optional[str] = None
    quant: Optional[str] = None
    active: bool = False


class CorePackContent(BaseModel):
    id: str
    filename: Optional[str] = None
    kind: Optional[str] = None
    content_json: Optional[Any] = None
    content_text: Optional[str] = None


class ActiveModelRequest(BaseModel):
    kind: str  # "llm" | "embedding"
    core_id: str


class ActiveModelStatus(BaseModel):
    active_llm: Optional[str] = None
    active_embedding: Optional[str] = None
    llm_loaded: bool = False
    embedding_loaded: bool = False
    llm_backend_available: bool = False
    llm_unavailable_reason: Optional[str] = None
    embedding_unavailable_reason: Optional[str] = None


class SystemInfo(BaseModel):
    platform: str
    total_ram_gb: float
    available_ram_gb: float
    cpu_cores: int
    gpu_detected: bool
    gpu_source: Optional[str] = None
    ram_detected: bool  # False means the fallback estimate was used, not a real reading


class ModelRecommendationItem(BaseModel):
    id: str
    name: Optional[str] = None
    kind: Optional[str] = None
    params: Optional[str] = None
    quant: Optional[str] = None
    size_bytes: int = 0
    required_ram_gb: float
    ram_fit: str  # "comfortable" | "tight" | "insufficient" | "unknown"
    speed_label: str  # "fast" | "medium" | "slow"
    qualifies: bool
    in_db: bool
    active: bool
    recommended: bool
    family: Optional[str] = None
    score: float = 0.0  # small-model ranking score (params + Chinese ability), higher = better
    engine_ok: Optional[bool] = None  # False = installed llama-cpp-python too old for this model
    min_llama_cpp: Optional[str] = None
    origin: Optional[str] = None  # "cn" | "overseas"


class ModelRecommendResponse(BaseModel):
    system: SystemInfo
    llm_recommendations: list[ModelRecommendationItem]
    embedding_recommendations: list[ModelRecommendationItem]
    recommended_llm_id: Optional[str] = None
    recommended_embedding_id: Optional[str] = None
    prefer: str = "small"
    small_llm_ids: list[str] = []  # top small models for this machine, best first
    llama_cpp_version: Optional[str] = None


# ---------- Runtime local-AI package installer (AI Center -> Models) ----------
class PyPackageOut(BaseModel):
    id: str
    name: str
    name_en: str
    description: str
    description_en: str
    import_name: str
    installed: bool
    version: Optional[str] = None


class PyPackageInstallResult(BaseModel):
    package_id: str
    installed: bool
    version: Optional[str] = None
    log_tail: str


class DiagnosisOut(BaseModel):
    """一条已落库的诊断(含依据快照: 知识库摘录 / 建议 SOP / 证据统计 / 校准说明)。"""

    model_config = {"from_attributes": True}
    id: int
    device_id: int
    alarm_id: Optional[int] = None
    component_name: Optional[str] = None
    symptom: Optional[str] = None
    diagnosis: Optional[str] = None
    confidence: Optional[float] = None
    recommendation: Optional[str] = None
    source: Optional[str] = None
    evidence: Optional[dict] = None
    created_at: Optional[datetime] = None


class FeedbackRateIn(BaseModel):
    verdict: str  # correct / partially_correct / incorrect
    actual_root_cause: Optional[str] = None


class FeedbackOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    diagnosis_id: int
    work_order_id: Optional[int] = None
    maintenance_record_id: Optional[int] = None
    verdict: str
    outcome: Optional[str] = None
    actual_root_cause: Optional[str] = None
    actual_action: Optional[str] = None
    knowledge_document_id: Optional[int] = None
    created_at: Optional[datetime] = None
    rated_at: Optional[datetime] = None


class FeedbackStats(BaseModel):
    component_name: str
    symptom_family: Optional[str] = None
    total: int
    rated: int
    unrated: int
    correct: int
    partially_correct: int
    incorrect: int
    accuracy: Optional[float] = None
