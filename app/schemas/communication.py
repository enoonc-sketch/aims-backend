from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CommTestRequest(BaseModel):
    host: str = Field(..., description="IP 地址或主机名")
    port: Optional[int] = Field(None, description="端口，留空则按协议使用默认端口")
    protocol: Optional[str] = Field(None, description="通讯协议，如 Modbus TCP / MQTT / OPC UA")
    timeout: float = Field(3.0, ge=0.5, le=15, description="超时时间（秒）")


class CommTestResult(BaseModel):
    success: bool
    latency_ms: Optional[float] = None
    message: str
    detail: Optional[str] = None


class IngestPoint(BaseModel):
    tag_address: str = Field(..., description="TAG 上配置的 tag_address(Modbus 寄存器 / OPC 节点 ID …)")
    value: float
    time: Optional[datetime] = Field(None, description="采集时间; 缺省为服务端收到的时刻(按实时数据处理)")
    quality: str = Field("good", description="设备/网关自己给的质量: good / uncertain / bad")


class GatewayIngestIn(BaseModel):
    gateway_code: str
    points: list[IngestPoint] = Field(..., min_length=1, max_length=5000)


class GatewayIngestOut(BaseModel):
    gateway_code: str
    accepted: int
    duplicates_skipped: int = 0
    unmapped_addresses: list[str] = Field(default_factory=list, description="该网关下没有任何 TAG 绑定的地址(未入库)")
    quality: dict = Field(default_factory=dict)
    alarms_created: int = 0
    data_quality_alarms_created: int = 0
    comm_alarms_resolved: int = 0
    work_orders_created: int = 0


class StaleScanOut(BaseModel):
    stale_minutes: int
    checked_tags: int
    stale_tags: list[dict] = Field(default_factory=list)
    never_reported_tags: int = 0
    comm_alarms_created: int = 0
    gateways_marked_offline: list[str] = Field(default_factory=list)
    devices_marked_offline: list[str] = Field(default_factory=list)
