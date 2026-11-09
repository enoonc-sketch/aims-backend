"""Dashboard API — aggregated KPIs shown on the platform's home page."""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.alarm import Alarm
from app.models.device import Device
from app.models.maintenance import WorkOrder
from app.schemas.dashboard import DashboardStats, DeviceHealthPoint

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStats)
def get_stats(db: Session = Depends(get_db)):
    total_devices = db.scalar(select(func.count()).select_from(Device)) or 0
    online_devices = db.scalar(select(func.count()).where(Device.status == "online")) or 0
    warning_devices = db.scalar(select(func.count()).where(Device.status == "warning")) or 0
    fault_devices = db.scalar(select(func.count()).where(Device.status == "fault")) or 0

    # SQLite-friendly: compare date portion via cast/string; Postgres also accepts this
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    alarms_today = db.scalar(
        select(func.count()).select_from(Alarm).where(Alarm.triggered_at >= today_start)
    ) or 0

    open_work_orders = db.scalar(
        select(func.count()).select_from(WorkOrder).where(WorkOrder.status.in_(["open", "in_progress"]))
    ) or 0

    return DashboardStats(
        total_devices=total_devices,
        online_devices=online_devices,
        warning_devices=warning_devices,
        fault_devices=fault_devices,
        alarms_today=alarms_today,
        open_work_orders=open_work_orders,
    )


@router.get("/health-overview", response_model=list[DeviceHealthPoint])
def get_health_overview(db: Session = Depends(get_db)):
    """Per-device health score, used by the Dashboard health chart."""
    devices = db.scalars(select(Device)).all()
    return [
        DeviceHealthPoint(device_code=d.device_code, health_score=float(d.health_score), status=d.status)
        for d in devices
    ]
