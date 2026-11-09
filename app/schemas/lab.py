from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class LabSampleCreate(BaseModel):
    device_id: Optional[int] = None
    component_name: Optional[str] = None
    sample_no: Optional[str] = None
    lab_value: Optional[float] = None
    online_value: Optional[float] = None
    created_by: Optional[int] = None


class LabSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: Optional[int] = None
    component_name: Optional[str] = None
    sample_no: Optional[str] = None
    lab_value: Optional[float] = None
    online_value: Optional[float] = None
    sampled_at: Optional[datetime] = None
    created_by: Optional[int] = None


class LabCorrelationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: Optional[int] = None
    component_name: Optional[str] = None
    sample_count: int = 0
    r_squared: Optional[float] = None
    slope: Optional[float] = None
    intercept: Optional[float] = None
    mean_deviation: Optional[float] = None
    computed_at: Optional[datetime] = None


class LabCorrelationRequest(BaseModel):
    device_id: int
    component_name: str
