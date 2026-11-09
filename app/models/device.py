"""Device domain models: models, devices, components, gateways (6 tables)."""
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class DeviceModel(Base):
    __tablename__ = "device_models"

    id = Column(Integer, primary_key=True)
    model_name = Column(String(100), nullable=False)
    manufacturer = Column(String(100))
    analyzer_type = Column(String(50))
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True)
    device_code = Column(String(50), unique=True, nullable=False)
    name = Column(String(150), nullable=False)
    model_id = Column(Integer, ForeignKey("device_models.id"))
    protocol = Column(String(30), nullable=False, default="Modbus TCP")
    location = Column(String(200))
    status = Column(String(20), nullable=False, default="offline")
    health_score = Column(Numeric(5, 2), nullable=False, default=100)
    ip_address = Column(String(64))
    installed_at = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    components = relationship("DeviceComponent", back_populates="device", cascade="all, delete-orphan")
    model = relationship("DeviceModel")

    __table_args__ = (Index("idx_devices_status", "status"),)


class DeviceComponent(Base):
    """A measured component/parameter of a device — i.e. its TAG. Carries the
    communication-layer link (gateway_id + tag_address) so the chain
    设备(Device) -> TAG(DeviceComponent) -> 通讯(Gateway) is a real FK path,
    not just an implicit naming convention. gateway_id/tag_address are
    optional: a tag can exist before its comms are configured."""

    __tablename__ = "device_components"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100), nullable=False)
    unit = Column(String(20), nullable=False, default="%")
    is_major = Column(Boolean, nullable=False, default=True)
    low_limit = Column(Numeric(12, 4))
    high_limit = Column(Numeric(12, 4))
    gateway_id = Column(Integer, ForeignKey("gateways.id", ondelete="SET NULL"))
    tag_address = Column(String(100))  # e.g. Modbus register / OPC node id for this tag

    device = relationship("Device", back_populates="components")
    gateway = relationship("Gateway")


class Gateway(Base):
    __tablename__ = "gateways"

    id = Column(Integer, primary_key=True)
    gateway_code = Column(String(50), unique=True, nullable=False)
    name = Column(String(150), nullable=False)
    ip_address = Column(String(64))
    port = Column(Integer)
    protocol = Column(String(30), default="MQTT")
    status = Column(String(20), nullable=False, default="offline")
    last_heartbeat = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GatewayDevice(Base):
    __tablename__ = "gateway_devices"

    gateway_id = Column(Integer, ForeignKey("gateways.id", ondelete="CASCADE"), primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)


class DeviceCalibrationRecord(Base):
    __tablename__ = "device_calibration_records"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100))
    calibrated_at = Column(DateTime(timezone=True), server_default=func.now())
    calibration_gas = Column(String(100))
    result = Column(String(20))
    deviation = Column(Numeric(10, 4))
    operator = Column(String(100))
    notes = Column(Text)
