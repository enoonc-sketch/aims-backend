"""Schemas for AI Center's device search / 选型 / cross-menu context endpoints."""
from typing import Optional

from pydantic import BaseModel, Field


class ReasonOut(BaseModel):
    code: str
    ok: bool
    zh: str
    en: str


class DeviceHit(BaseModel):
    id: int
    device_code: str
    name: str
    status: str
    health_score: float = 0
    protocol: Optional[str] = None
    location: Optional[str] = None
    ip_address: Optional[str] = None
    model_name: Optional[str] = None
    manufacturer: Optional[str] = None
    analyzer_type: Optional[str] = None
    components: list[str] = []
    gateways: list[str] = []
    asset_code: Optional[str] = None
    active_alarms: int = 0
    open_work_orders: int = 0
    points_24h: int = 0
    lab_samples: int = 0
    score: float = 0
    matched_in: list[str] = []


class ParsedQueryOut(BaseModel):
    """What the search parser understood from the free-text query."""

    terms: list[str] = []
    components: list[str] = []
    statuses: list[str] = []
    analyzer_types: list[str] = []
    protocols: list[str] = []


class DeviceSearchResponse(BaseModel):
    query: str
    parsed: ParsedQueryOut
    total: int
    items: list[DeviceHit]


class DeviceSelectRequest(BaseModel):
    # Natural language ("需要能测H2S和CO2的在线色谱") and/or explicit fields — merged.
    query: Optional[str] = None
    components: list[str] = []
    analyzer_type: Optional[str] = None
    protocol: Optional[str] = None
    online_only: bool = False
    min_health: float = Field(0, ge=0, le=100)
    location: Optional[str] = None
    limit: int = Field(5, ge=1, le=20)


class RequirementOut(BaseModel):
    components: list[str] = []
    analyzer_types: list[str] = []
    protocols: list[str] = []
    online_only: bool = False
    min_health: float = 0
    location: Optional[str] = None
    keywords: list[str] = []


class SelectedDevice(DeviceHit):
    fit: str  # full | partial | none | n/a  (component coverage)
    missing_components: list[str] = []
    reasons: list[ReasonOut] = []


class ModelRecommendation(BaseModel):
    model_name: str
    manufacturer: Optional[str] = None
    analyzer_type: Optional[str] = None
    units: int
    avg_health: float
    online_rate: float
    avg_active_alarms: float
    avg_open_work_orders: float
    protocols: list[str] = []
    components: list[str] = []
    device_codes: list[str] = []
    score: float
    fit: str
    missing_components: list[str] = []
    reasons: list[ReasonOut] = []


class DeviceSelectResponse(BaseModel):
    requirement: RequirementOut
    total_devices: int
    considered: int  # devices that passed the hard constraints (online-only / min health)
    devices: list[SelectedDevice]
    models: list[ModelRecommendation]
    basis_zh: str
    basis_en: str


class DeviceLinkOut(BaseModel):
    menu: str  # front-end page id: dev / comm / danal / dash / maint / asset / lab
    label_zh: str
    label_en: str
    count: Optional[int] = None
    detail: Optional[str] = None


class DeviceContextOut(BaseModel):
    device: DeviceHit
    links: list[DeviceLinkOut]


class DeviceFacets(BaseModel):
    components: list[str] = []
    analyzer_types: list[str] = []
    protocols: list[str] = []
    statuses: list[str] = []
    models: list[str] = []
