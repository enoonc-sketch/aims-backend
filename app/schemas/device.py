from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DeviceComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    component_name: str
    unit: str
    is_major: bool
    low_limit: Optional[float] = None
    high_limit: Optional[float] = None
    gateway_id: Optional[int] = None
    tag_address: Optional[str] = None


class DeviceBase(BaseModel):
    device_code: str
    name: str
    model_id: Optional[int] = None
    protocol: str = "Modbus TCP"
    location: Optional[str] = None
    status: str = "offline"
    health_score: float = 100
    ip_address: Optional[str] = None
    installed_at: Optional[date] = None


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    protocol: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    health_score: Optional[float] = None
    ip_address: Optional[str] = None


class DeviceOut(DeviceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    components: list[DeviceComponentOut] = []


class TagConfigIn(BaseModel):
    """配置一个 TAG(设备的一个测点)及其通讯绑定: 设备 -> TAG -> 通讯。"""

    component_name: str
    unit: Optional[str] = None
    low_limit: Optional[float] = None    # 仪表量程(数据质量判断用), 不是报警阈值(报警阈值在 AlarmRule)
    high_limit: Optional[float] = None
    gateway_code: Optional[str] = None   # 经由哪个网关采集
    tag_address: Optional[str] = None    # 网关侧的地址: Modbus 寄存器 / OPC 节点 ID …
    is_major: bool = True
