from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AlarmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    rule_id: Optional[int] = None
    component_name: Optional[str] = None
    alarm_type: str = "process"
    severity: str
    message: str
    value: Optional[float] = None
    status: str
    acknowledged_by: Optional[int] = None
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    triggered_at: datetime


class AlarmCreate(BaseModel):
    device_id: int
    component_name: Optional[str] = None
    severity: str = "warning"
    message: str
    value: Optional[float] = None


class AlarmAck(BaseModel):
    """报警确认（兼容旧客户端）。user_id 不再被采信：确认人一律取当前登录用户，
    否则任何登录用户都能伪造"谁确认了这条报警"（审计完整性问题）。"""

    user_id: Optional[int] = None


class AlarmRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: Optional[int] = None
    component_name: Optional[str] = None
    rule_type: str
    low_limit: Optional[float] = None
    high_limit: Optional[float] = None
    severity: str
    is_enabled: bool


class AlarmRuleCreate(BaseModel):
    device_id: int
    component_name: str
    rule_type: str = "threshold"
    low_limit: Optional[float] = None
    high_limit: Optional[float] = None
    severity: str = "warning"
