"""Measurement domain models (2 tables). `measurements` is a TimescaleDB hypertable."""
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, func

from app.db.base import Base


class MeasurementImport(Base):
    __tablename__ = "measurement_imports"

    id = Column(Integer, primary_key=True)
    file_name = Column(String(255), nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id"))
    row_count = Column(Integer, nullable=False, default=0)
    column_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="processing")
    # 数据来源: "local"=数据管理页手动导入, "cloud"=云数据(外部数据源)同步。
    # 云数据同步产生的批次会记下是哪个外部数据库，数据管理页据此展示/筛选来源。
    source = Column(String(20), nullable=False, default="local", server_default="local")
    external_database_id = Column(Integer, ForeignKey("external_databases.id", ondelete="SET NULL"))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Measurement(Base):
    """Time-series table. Primary key is (time, device_id, component_name) at the DB
    level via the hypertable partitioning; no surrogate id column by design."""
    __tablename__ = "measurements"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    component_name = Column(String(100), primary_key=True, nullable=False)
    value = Column(Float, nullable=False)
    quality = Column(String(20), nullable=False, default="good")
    import_id = Column(Integer, ForeignKey("measurement_imports.id"))

    __table_args__ = (
        Index("idx_measurements_device_time", "device_id", "time"),
        Index("idx_measurements_component", "component_name"),
    )
