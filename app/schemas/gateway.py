from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class GatewayBase(BaseModel):
    gateway_code: str
    name: str
    ip_address: Optional[str] = None
    port: Optional[int] = None
    protocol: str = "MQTT"
    status: str = "offline"


class GatewayCreate(GatewayBase):
    pass


class GatewayUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    status: Optional[str] = None


class GatewayOut(GatewayBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    last_heartbeat: Optional[datetime] = None
    created_at: datetime
