from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_devices: int
    online_devices: int
    warning_devices: int
    fault_devices: int
    alarms_today: int
    open_work_orders: int


class DeviceHealthPoint(BaseModel):
    device_code: str
    health_score: float
    status: str
