"""Data Analysis / Dashboard / Notification domain models (3 tables)."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, func

from app.db.base import Base


class DataAnalysisDataset(Base):
    __tablename__ = "data_analysis_datasets"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    source_import_id = Column(Integer, ForeignKey("measurement_imports.id"))
    x_field = Column(String(100))
    y_fields = Column(JSON)
    filters = Column(JSON)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DashboardSnapshot(Base):
    __tablename__ = "dashboard_snapshots"

    id = Column(Integer, primary_key=True)
    total_devices = Column(Integer, nullable=False, default=0)
    online_devices = Column(Integer, nullable=False, default=0)
    alarms_today = Column(Integer, nullable=False, default=0)
    faults = Column(Integer, nullable=False, default=0)
    open_work_orders = Column(Integer, nullable=False, default=0)
    snapshot_at = Column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    title = Column(String(200), nullable=False)
    body = Column(Text)
    is_read = Column(Boolean, nullable=False, default=False)
    related_entity = Column(String(50))
    related_id = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
