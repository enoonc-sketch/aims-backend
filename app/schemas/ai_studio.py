"""Schemas for the AI-generated SOP / ROI, Data Analysis reading, knowledge Q&A and the
data-analysis-driven Lab comparison + AI certification."""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Lang = Literal["zh", "en"]


class SourceRef(BaseModel):
    type: str  # knowledge / sop / diagnosis / correlation
    title: str
    id: Optional[int] = None
    document_id: Optional[int] = None
    score: Optional[float] = None
    excerpt: Optional[str] = None


class Message(BaseModel):
    code: str
    severity: str = "info"
    zh: str
    en: str


# ------------------------------------------------------------------ SOP ----
class SOPGenerateRequest(BaseModel):
    device_id: int
    sop_type: str = Field(..., description="Calibration / Replacement / Maintenance (case-insensitive)")
    component_name: Optional[str] = None
    lang: Lang = "zh"
    save: bool = Field(False, description="Store the draft in maintenance_sop (version 'AI-draft')")


class SOPGenerateResult(BaseModel):
    device_id: int
    device_code: str
    device_model_id: Optional[int] = None  # so the reviewed text can be saved via POST /maintenance/sop
    sop_type: str
    title: str
    content: str
    lang: str
    status: str = "draft"  # always a draft: needs human approval
    generated_by: str  # llm | template
    sources: list[SourceRef] = []
    context: dict[str, Any] = {}
    notes: list[str] = []
    saved_sop_id: Optional[int] = None


# ------------------------------------------------------------------ ROI ----
class ROIRequest(BaseModel):
    device_id: int
    period_months: int = Field(12, ge=1, le=120)
    annual_benefit: Optional[float] = Field(None, ge=0, description="Your estimate of the yearly benefit; needed for ROI % and payback")
    lang: Lang = "zh"


class ROIResult(BaseModel):
    device_id: int
    device_code: str
    asset_code: Optional[str] = None
    metrics: dict[str, Any]
    flags: list[Message] = []
    data_gaps: list[Message] = []
    assumptions: list[Message] = []
    formulas: list[Message] = []
    thresholds: dict[str, Any] = {}
    narrative: str
    generated_by: str
    notes: list[str] = []


# ------------------------------------------------------- analysis reading ----
class AnalysisReadRequest(BaseModel):
    # Either server data (device_id [+ components, hours]) or inline series (e.g. the sheet
    # currently open on the Data Analysis page).
    device_id: Optional[int] = None
    components: list[str] = []
    hours: int = Field(24, ge=1, le=24 * 90)
    series: Optional[dict[str, list[Optional[float]]]] = None
    check_sum: bool = False
    lang: Lang = "zh"


class Finding(BaseModel):
    severity: str
    code: str
    series: Optional[str] = None
    zh: str
    en: str
    evidence: dict[str, Any] = {}


class AnalysisReadResult(BaseModel):
    source: str  # server | inline
    device_id: Optional[int] = None
    hours: Optional[int] = None
    series: list[dict[str, Any]]
    findings: list[Finding]
    correlations: list[dict[str, Any]] = []
    sum_check: Optional[dict[str, Any]] = None
    thresholds: dict[str, Any] = {}
    narrative: str
    generated_by: str
    notes: list[str] = []


# ------------------------------------------------------------ knowledge ----
class KnowledgeAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    device_id: Optional[int] = Field(None, description="Also ground the answer in this device's live platform data")
    limit: int = Field(5, ge=1, le=10)
    lang: Lang = "zh"


class KnowledgeAskResult(BaseModel):
    answer: str
    generated_by: str  # llm | extractive | none
    grounded: bool  # False = nothing relevant found; the answer says so instead of guessing
    sources: list[SourceRef] = []
    live_context_used: bool = False
    notes: list[str] = []


class KnowledgeRef(BaseModel):
    """A knowledge-base excerpt attached to an AI result (diagnosis, SOP...)."""

    document_id: int
    document_title: str
    chunk_id: int
    score: float
    excerpt: str


# ------------------------------------------------------------------- Lab ----
class LabCompareRequest(BaseModel):
    device_id: int
    component_name: str
    window_minutes: float = Field(30, gt=0, le=1440, description="Online value = analyzer data within ±this many minutes of the sampling time")
    aggregate: Literal["mean", "median", "nearest"] = "mean"
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


class LabPair(BaseModel):
    sample_id: Optional[int] = None
    sample_no: Optional[str] = None
    sampled_at: Optional[str] = None
    lab: float
    online: float
    n_points: int
    nearest_delta_min: float
    diff: float
    diff_pct: Optional[float] = None


class LabUnmatched(BaseModel):
    sample_id: Optional[int] = None
    sample_no: Optional[str] = None
    sampled_at: Optional[str] = None
    reason: str  # no_data_in_window | no_lab_value | no_time


class LabCompareResult(BaseModel):
    device_id: int
    device_code: str
    component_name: str
    window_minutes: float
    aggregate: str
    source: str = "analysis"  # online values come from measurement (Data Analysis) data
    sample_count: int  # lab samples considered
    pairs: list[LabPair]
    unmatched: list[LabUnmatched]
    stats: dict[str, Any]


class CheckOut(BaseModel):
    code: str
    ok: bool
    hard: bool
    value: Any = None
    threshold: Any = None
    zh: str
    en: str


class LabCertifyRequest(LabCompareRequest):
    criteria: Optional[dict[str, Any]] = Field(None, description="Override any of the default acceptance criteria")
    valid_days: int = Field(30, ge=1, le=365)
    lang: Lang = "zh"
    persist: bool = True


class LabCertifyResult(LabCompareResult):
    status: str  # certified | conditional | failed | insufficient
    score: float
    checks: list[CheckOut]
    criteria: dict[str, Any]
    disclaimer: dict[str, str]
    narrative: str
    generated_by: str
    notes: list[str] = []
    certification_id: Optional[int] = None
    valid_until: Optional[datetime] = None


class LabCertificationOut(BaseModel):
    id: int
    device_id: int
    device_code: Optional[str] = None
    component_name: str
    status: str
    score: Optional[float] = None
    window_minutes: int
    sample_count: int
    pair_count: int
    r_squared: Optional[float] = None
    bias_pct: Optional[float] = None
    mape_pct: Optional[float] = None
    generated_by: Optional[str] = None
    narrative: Optional[str] = None
    checks: Optional[list[dict[str, Any]]] = None
    certified_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    expired: bool = False
    disclaimer: dict[str, str] = {}
