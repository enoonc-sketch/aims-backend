from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


# ---------- Work Orders ----------
class WorkOrderBase(BaseModel):
    device_id: Optional[int] = None
    alarm_id: Optional[int] = None
    title: str
    order_type: str = "repair"          # repair/calibration/inspection/replacement
    priority: str = "medium"            # low/medium/high/urgent
    status: str = "open"                # open/in_progress/done/cancelled
    assigned_to: Optional[int] = None
    created_by: Optional[int] = None
    due_date: Optional[date] = None


class WorkOrderCreate(WorkOrderBase):
    pass


class WorkOrderUpdate(BaseModel):
    title: Optional[str] = None
    order_type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[int] = None
    due_date: Optional[date] = None


class WorkOrderOut(WorkOrderBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    closed_at: Optional[datetime] = None
    created_at: datetime
    description: Optional[str] = None      # 报警自动建单时: AI 诊断 + 知识库摘录 + 建议 SOP
    diagnosis_id: Optional[int] = None
    sop_id: Optional[int] = None


# ---------- Maintenance Records ----------
class MaintenanceRecordCreate(BaseModel):
    work_order_id: Optional[int] = None
    device_id: Optional[int] = None
    action_taken: Optional[str] = None
    parts_used: Optional[str] = None
    cost: Optional[float] = None
    performed_by: Optional[int] = None
    outcome: Optional[Literal["fixed", "partially_fixed", "not_fixed", "no_fault_found"]] = None
    root_cause: Optional[str] = None


class MaintenanceRecordOut(MaintenanceRecordCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    performed_at: datetime


# ---------- Maintenance SOP ----------
class MaintenanceSOPCreate(BaseModel):
    device_model_id: Optional[int] = None
    sop_type: str                       # Calibration/Replacement/Maintenance
    title: str
    content: str
    version: str = "1.0"


class MaintenanceSOPOut(MaintenanceSOPCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


# ---------- Spare Parts ----------
class SparePartBase(BaseModel):
    part_code: str
    name: str
    device_model_id: Optional[int] = None
    stock_qty: int = 0
    unit_price: Optional[float] = None
    supplier: Optional[str] = None


class SparePartCreate(SparePartBase):
    pass


class SparePartUpdate(BaseModel):
    name: Optional[str] = None
    stock_qty: Optional[int] = None
    unit_price: Optional[float] = None
    supplier: Optional[str] = None


class SparePartOut(SparePartBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    updated_at: datetime


class SparePartStockAdjust(BaseModel):
    delta: int   # positive = stock in, negative = stock out
    reason: Optional[str] = None


class WorkOrderComplete(BaseModel):
    """工单完成: 一次性提交维修结果 + 对 AI 诊断的复盘。"""

    action_taken: str
    outcome: Literal["fixed", "partially_fixed", "not_fixed", "no_fault_found"]
    root_cause: Optional[str] = None
    parts_used: Optional[str] = None
    cost: Optional[float] = None
    performed_by: Optional[int] = None
    # AI 诊断对不对由工程师判断; 不填 = 未评价(不会影响之后的置信度校准, 但会出现在待评价列表里)
    diagnosis_verdict: Optional[Literal["correct", "partially_correct", "incorrect"]] = None


class WorkOrderCompleteOut(BaseModel):
    work_order: WorkOrderOut
    record: MaintenanceRecordOut
    alarm_id: Optional[int] = None
    alarm_status: Optional[str] = None
    feedback_id: Optional[int] = None
    knowledge_document_id: Optional[int] = None  # 写回知识库的维修案例
