from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DatasetCreate(BaseModel):
    name: str
    source_import_id: Optional[int] = None
    x_field: Optional[str] = None
    y_fields: Optional[list[str]] = None
    filters: Optional[dict[str, Any]] = None
    created_by: Optional[int] = None


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    source_import_id: Optional[int] = None
    x_field: Optional[str] = None
    y_fields: Optional[list[str]] = None
    filters: Optional[dict[str, Any]] = None
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None


class SeriesStats(BaseModel):
    component_name: str
    count: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    std: Optional[float] = None
    sum: Optional[float] = None
    last: Optional[float] = None


class AnalysisCoresRequest(BaseModel):
    """Request for multi-component statistical cores on measurement data."""
    device_id: int
    components: list[str] = Field(default_factory=list, description="Empty = all components for device")
    hours: int = Field(24, ge=1, le=24 * 90)
    include_sum: bool = True


class AnalysisCoresResponse(BaseModel):
    device_id: int
    hours: int
    series: list[SeriesStats]
    component_sum: Optional[SeriesStats] = None  # sum of selected components per sample time (avg of sums)
    sample_count: int = 0


class MultiTrendRequest(BaseModel):
    device_id: int
    components: list[str]
    hours: int = 24
    max_points: int = Field(500, ge=10, le=5000)


class MultiTrendSeries(BaseModel):
    component_name: str
    points: list[dict[str, Any]]  # {time, value}


class MultiTrendResponse(BaseModel):
    device_id: int
    hours: int
    series: list[MultiTrendSeries]
