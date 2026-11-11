"""Maintenance API — work orders, maintenance records, SOP library, spare parts.

Corresponds to the prototype's "Maintenance" page (SOP / Fault PDF / ROI tabs)
plus the underlying work-order tracking that isn't visible as a dedicated tab
but is needed to back alarms -> work orders -> maintenance records.
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.device import Device, DeviceModel
from app.models.maintenance import MaintenanceRecord, MaintenanceSOP, SparePart, WorkOrder
from app.models.system import User
from app.schemas.imports import TableImportResult
from app.schemas.maintenance import (
    MaintenanceRecordCreate,
    MaintenanceRecordOut,
    MaintenanceSOPCreate,
    MaintenanceSOPOut,
    SparePartCreate,
    SparePartOut,
    SparePartStockAdjust,
    SparePartUpdate,
    WorkOrderComplete,
    WorkOrderCompleteOut,
    WorkOrderCreate,
    WorkOrderOut,
    WorkOrderUpdate,
)
from app.services import table_import
from app.services.upload_security import read_upload

router = APIRouter(prefix="/maintenance", tags=["Maintenance"])

_WORK_ORDER_IMPORT_SPEC = {
    "title": ("title", "标题", "工单标题"),
    "device_code": ("device_code", "设备编号", "设备"),
    "order_type": ("order_type", "type", "类型", "工单类型"),
    "priority": ("priority", "优先级"),
    "status": ("status", "状态"),
    "due_date": ("due_date", "截止日期", "计划日期"),
}


# ============================================================
# Work Orders
# ============================================================
@router.get("/work-orders", response_model=list[WorkOrderOut])
def list_work_orders(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    status: Optional[str] = Query(None, description="open/in_progress/done/cancelled"),
    priority: Optional[str] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(WorkOrder)
    if device_id:
        stmt = stmt.where(WorkOrder.device_id == device_id)
    if status:
        stmt = stmt.where(WorkOrder.status == status)
    if priority:
        stmt = stmt.where(WorkOrder.priority == priority)
    stmt = stmt.order_by(WorkOrder.created_at.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.get("/work-orders/{work_order_id}", response_model=WorkOrderOut)
def get_work_order(work_order_id: int, db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    return wo


@router.post("/work-orders", response_model=WorkOrderOut, status_code=201)
def create_work_order(payload: WorkOrderCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.device_id and not db.get(Device, payload.device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    wo = WorkOrder(**payload.model_dump())
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


@router.patch("/work-orders/{work_order_id}", response_model=WorkOrderOut)
def update_work_order(work_order_id: int, payload: WorkOrderUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    wo = db.get(WorkOrder, work_order_id)
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    from datetime import datetime, timezone

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(wo, field, value)
    # PATCH status=done 以前不写 closed_at, 工单显示"已完成"却没有完成时间
    if wo.status in ("done", "cancelled") and wo.closed_at is None:
        wo.closed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(wo)
    return wo


@router.post("/work-orders/{work_order_id}/close", response_model=WorkOrderOut)
def close_work_order(work_order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """行政性关闭(不记录维修结果)。要走完 维修结果 -> AI反馈 闭环, 请用 /complete。

    如果这张工单带着 AI 诊断, 这里会登记一条 verdict=unrated 的反馈, 让它出现在
    GET /ai/feedback/pending 里, 而不是悄悄从闭环里掉出去。"""
    from datetime import datetime, timezone

    from app.services import repair_feedback

    wo = db.get(WorkOrder, work_order_id)
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    wo.status = "done"
    wo.closed_at = datetime.now(timezone.utc)
    if wo.diagnosis_id:
        repair_feedback.record_feedback(
            db, diagnosis_id=wo.diagnosis_id, work_order=wo, record=None,
            verdict=None, outcome=None, root_cause=None, action_taken=None,
        )
    db.commit()
    db.refresh(wo)
    return wo


@router.post("/work-orders/{work_order_id}/complete", response_model=WorkOrderCompleteOut)
def complete_work_order(
    work_order_id: int,
    payload: WorkOrderComplete,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单 -> 维修结果 -> AI反馈, 一步完成:

    1. 写维修记录(做了什么 / 用料 / 费用 / 结果 / 实际根因);
    2. 结果为 fixed / no_fault_found → 工单 done 并关闭其报警; partially_fixed / not_fixed → 工单保持 in_progress
       (这是一次维修尝试, 不是结案), 报警不动;
    3. 若工单带 AI 诊断 → 写一条复盘反馈(diagnosis_verdict 由工程师给, 不给则 unrated);
    4. 修复成功且有根因/处理措施 → 把案例写回知识库, 下次同类报警的知识库检索能命中。
    """
    from datetime import datetime, timezone

    from app.models.ai import AIDiagnosis
    from app.models.alarm import Alarm
    from app.services import repair_feedback

    wo = db.get(WorkOrder, work_order_id)
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    if wo.status in ("done", "cancelled", "closed"):
        raise HTTPException(status_code=409, detail=f"Work order already {wo.status}")

    now = datetime.now(timezone.utc)
    record = MaintenanceRecord(
        work_order_id=wo.id,
        device_id=wo.device_id,
        action_taken=payload.action_taken,
        parts_used=payload.parts_used,
        cost=payload.cost,
        performed_by=payload.performed_by or user.id,
        outcome=payload.outcome,
        root_cause=payload.root_cause,
    )
    db.add(record)
    db.flush()

    alarm = db.get(Alarm, wo.alarm_id) if wo.alarm_id else None
    if payload.outcome in ("fixed", "no_fault_found"):
        wo.status = "done"
        wo.closed_at = now
        if alarm is not None and alarm.status != "resolved":
            alarm.status = "resolved"
            alarm.resolved_at = now
    else:
        wo.status = "in_progress"

    fb = None
    kb_doc_id = None
    if wo.diagnosis_id:
        diagnosis = db.get(AIDiagnosis, wo.diagnosis_id)
        fb = repair_feedback.record_feedback(
            db, diagnosis_id=wo.diagnosis_id, work_order=wo, record=record,
            verdict=payload.diagnosis_verdict, outcome=payload.outcome,
            root_cause=payload.root_cause, action_taken=payload.action_taken,
        )
        if diagnosis is not None:
            doc = repair_feedback.write_case_to_kb(db, fb, diagnosis, wo, record, user_id=user.id)
            kb_doc_id = doc.id if doc is not None else None

    db.commit()
    db.refresh(wo)
    db.refresh(record)
    return WorkOrderCompleteOut(
        work_order=wo,
        record=record,
        alarm_id=alarm.id if alarm is not None else None,
        alarm_status=alarm.status if alarm is not None else None,
        feedback_id=fb.id if fb is not None else None,
        knowledge_document_id=kb_doc_id,
    )


@router.get("/work-orders/import/template")
def download_work_orders_import_template():
    """Downloadable .xlsx template for 维护管理 -> 批量导入."""
    content = table_import.build_excel_template(
        ["标题", "设备编号", "类型", "优先级", "状态", "截止日期"],
        [["更换采样探头", "DEV-001", "repair", "high", "open", "2026-10-01"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="工单导入模板.xlsx"),
    )


@router.post("/work-orders/import", response_model=TableImportResult, status_code=201)
async def import_work_orders(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import work orders from an uploaded CSV/Excel file (维护管理 -> 批量导入).

    Auto-detects title/device_code/order_type/priority/status/due_date columns
    (English or Chinese headers). `device_code`, if present, links the work
    order to an existing device. Append-only (work orders have no natural
    unique key to upsert on), same as the measurements/lab importers.
    """
    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename = upload.raw, upload.filename

    try:
        df = table_import.read_table(raw, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    columns_detected = table_import.detect_columns(df.columns, _WORK_ORDER_IMPORT_SPEC)
    if "title" not in columns_detected:
        raise HTTPException(status_code=400, detail="Could not detect required column title (标题)")

    warnings: list[str] = []
    inserted = skipped = 0
    device_cache: dict[str, Optional[int]] = {}
    valid_types = {"repair", "calibration", "inspection", "replacement"}
    valid_priorities = {"low", "medium", "high", "urgent"}
    valid_statuses = {"open", "in_progress", "done", "cancelled"}

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            title = table_import.clean_str(table_import.row_value(row, columns_detected, "title"))
            if not title:
                skipped += 1
                warnings.append(f"Row {i + 2}: missing title — skipped")
                continue

            device_id = None
            device_code = table_import.clean_str(table_import.row_value(row, columns_detected, "device_code"))
            if device_code:
                if device_code not in device_cache:
                    dev = db.scalar(select(Device).where(Device.device_code == device_code))
                    device_cache[device_code] = dev.id if dev else None
                    if dev is None:
                        warnings.append(f"Row {i + 2}: device_code '{device_code}' not found — work order left unlinked")
                device_id = device_cache[device_code]

            order_type = (table_import.clean_str(table_import.row_value(row, columns_detected, "order_type")) or "repair").lower()
            if order_type not in valid_types:
                order_type = "repair"
            priority = (table_import.clean_str(table_import.row_value(row, columns_detected, "priority")) or "medium").lower()
            if priority not in valid_priorities:
                priority = "medium"
            status = (table_import.clean_str(table_import.row_value(row, columns_detected, "status")) or "open").lower()
            if status not in valid_statuses:
                status = "open"

            db.add(
                WorkOrder(
                    device_id=device_id,
                    title=title,
                    order_type=order_type,
                    priority=priority,
                    status=status,
                    due_date=table_import.clean_date(table_import.row_value(row, columns_detected, "due_date")),
                )
            )
            inserted += 1

        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Import failed while processing row {i + 2}: {e}")

    return TableImportResult(
        file_name=filename,
        row_count=len(df),
        columns_detected=columns_detected,
        inserted=inserted,
        updated=0,
        skipped=skipped,
        status="done",
        warnings=warnings,
    )


# ============================================================
# Maintenance Records
# ============================================================
@router.get("/records", response_model=list[MaintenanceRecordOut])
def list_records(
    db: Session = Depends(get_db),
    work_order_id: Optional[int] = None,
    device_id: Optional[int] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(MaintenanceRecord)
    if work_order_id:
        stmt = stmt.where(MaintenanceRecord.work_order_id == work_order_id)
    if device_id:
        stmt = stmt.where(MaintenanceRecord.device_id == device_id)
    stmt = stmt.order_by(MaintenanceRecord.performed_at.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/records", response_model=MaintenanceRecordOut, status_code=201)
def create_record(payload: MaintenanceRecordCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = MaintenanceRecord(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
# SOP library
# ============================================================
@router.get("/sop", response_model=list[MaintenanceSOPOut])
def list_sop(
    db: Session = Depends(get_db),
    device_model_id: Optional[int] = None,
    sop_type: Optional[str] = Query(None, description="Calibration/Replacement/Maintenance"),
):
    stmt = select(MaintenanceSOP)
    if device_model_id:
        stmt = stmt.where(MaintenanceSOP.device_model_id == device_model_id)
    if sop_type:
        stmt = stmt.where(MaintenanceSOP.sop_type == sop_type)
    return db.scalars(stmt).all()


@router.get("/sop/generate", response_model=MaintenanceSOPOut)
def generate_sop(
    db: Session = Depends(get_db),
    device_id: int = Query(..., description="Target device, e.g. Analyzer-001"),
    sop_type: str = Query(..., description="Calibration/Replacement/Maintenance"),
):
    """Look up the SOP for a device's model + procedure type.

    Mirrors the prototype's Maintenance > SOP tab, where the user picks a
    device and a procedure type and gets back the generated procedure text.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.model_id:
        raise HTTPException(status_code=404, detail="Device has no associated device model")

    sop = db.scalar(
        select(MaintenanceSOP).where(
            MaintenanceSOP.device_model_id == device.model_id,
            MaintenanceSOP.sop_type == sop_type,
        ).order_by(MaintenanceSOP.id)
    )
    if not sop:
        raise HTTPException(
            status_code=404,
            detail=f"No SOP found for model_id={device.model_id}, type={sop_type}",
        )
    return sop


@router.post("/sop", response_model=MaintenanceSOPOut, status_code=201)
def create_sop(payload: MaintenanceSOPCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.device_model_id and not db.get(DeviceModel, payload.device_model_id):
        raise HTTPException(status_code=404, detail="Device model not found")
    sop = MaintenanceSOP(**payload.model_dump())
    db.add(sop)
    db.commit()
    db.refresh(sop)
    return sop


# ============================================================
# Spare Parts
# ============================================================
@router.get("/spare-parts", response_model=list[SparePartOut])
def list_spare_parts(
    db: Session = Depends(get_db),
    device_model_id: Optional[int] = None,
    low_stock_below: Optional[int] = Query(None, description="Only return parts with stock_qty below this"),
):
    stmt = select(SparePart)
    if device_model_id:
        stmt = stmt.where(SparePart.device_model_id == device_model_id)
    if low_stock_below is not None:
        stmt = stmt.where(SparePart.stock_qty < low_stock_below)
    return db.scalars(stmt).all()


@router.post("/spare-parts", response_model=SparePartOut, status_code=201)
def create_spare_part(payload: SparePartCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.scalar(select(SparePart).where(SparePart.part_code == payload.part_code))
    if existing:
        raise HTTPException(status_code=409, detail="part_code already exists")
    part = SparePart(**payload.model_dump())
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


@router.patch("/spare-parts/{part_id}", response_model=SparePartOut)
def update_spare_part(part_id: int, payload: SparePartUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    part = db.get(SparePart, part_id)
    if not part:
        raise HTTPException(status_code=404, detail="Spare part not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(part, field, value)
    db.commit()
    db.refresh(part)
    return part


@router.post("/spare-parts/{part_id}/adjust-stock", response_model=SparePartOut)
def adjust_stock(part_id: int, payload: SparePartStockAdjust, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    part = db.get(SparePart, part_id)
    if not part:
        raise HTTPException(status_code=404, detail="Spare part not found")
    new_qty = part.stock_qty + payload.delta
    if new_qty < 0:
        raise HTTPException(status_code=400, detail="Resulting stock_qty cannot be negative")
    part.stock_qty = new_qty
    db.commit()
    db.refresh(part)
    return part


# ---------------------------------------------------------------------------
# 故障分析 PDF 解析（AIMS 1.0 阶段 7：替换前端"检测到 3 个故障模式"的假结果）
#
# 真实流程：上传 → 体积/类型校验（复用 upload_security）→ pypdf 提取文本层 →
# 抽取位号/报警码/测量值/故障关键词 → 规则包给候选诊断（只建议、不落库）。
# 扫描件无文本层时如实返回 422，不编造内容。
# ---------------------------------------------------------------------------
@router.post("/fault-pdf/analyze")
async def analyze_fault_pdf(
    file: UploadFile = File(...),
    component_name: str = Query("", description="可选：该 PDF 针对的测量组分，用于规则匹配"),
    user: User = Depends(get_current_user),
):
    from app.services import fault_pdf
    from app.services.upload_security import UploadRejected, read_upload

    try:
        upload = await read_upload(file, allowed_exts=("pdf",))
        return fault_pdf.analyze_pdf(upload.raw, filename=upload.filename, component_name=component_name)
    except UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except fault_pdf.PdfParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
