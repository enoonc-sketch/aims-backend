"""Alarm domain models (2 tables)."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func

from app.db.base import Base


class AlarmRule(Base):
    __tablename__ = "alarm_rules"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
    component_name = Column(String(100))
    rule_type = Column(String(30), nullable=False, default="threshold")
    low_limit = Column(Numeric(12, 4))
    high_limit = Column(Numeric(12, 4))
    severity = Column(String(20), nullable=False, default="warning")
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Alarm(Base):
    __tablename__ = "alarms"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    rule_id = Column(Integer, ForeignKey("alarm_rules.id"))
    component_name = Column(String(100))
    # process = 工艺阈值报警; data_quality = 连续坏质量数据; comm = 通讯中断(点位长时间无数据)
    alarm_type = Column(String(20), nullable=False, default="process", server_default="process")
    severity = Column(String(20), nullable=False, default="warning")
    message = Column(Text, nullable=False)
    value = Column(Numeric(12, 4))
    status = Column(String(20), nullable=False, default="active")
    acknowledged_by = Column(Integer, ForeignKey("users.id"))
    acknowledged_at = Column(DateTime(timezone=True))
    resolved_at = Column(DateTime(timezone=True))
    triggered_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_alarms_device_status", "device_id", "status"),
        Index("idx_alarms_triggered_at", "triggered_at"),
    )
