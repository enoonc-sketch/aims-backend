"""Gateways API — CRUD for communication gateways, plus a connectivity test
endpoint used by the Communication Management (通讯管理) page.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.device import Gateway
from app.models.system import User
from app.schemas.communication import CommTestResult
from app.schemas.gateway import GatewayCreate, GatewayOut, GatewayUpdate
from app.schemas.imports import TableImportResult
from app.services import comm_test, table_import
from app.services.upload_security import read_upload

router = APIRouter(prefix="/gateways", tags=["Gateways"])

_IMPORT_SPEC = {
    "gateway_code": ("gateway_code", "code", "网关编号", "编号", "网关代码"),
    "name": ("gateway_name", "name", "网关名称", "名称"),
    "ip_address": ("ip_address", "ip", "ip地址"),
    "port": ("port", "端口"),
    "protocol": ("protocol", "通讯协议", "协议"),
    "status": ("status", "状态"),
}


@router.get("", response_model=list[GatewayOut])
def list_gateways(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by gateway code or name"),
    status: Optional[str] = Query(None, description="Filter by status: online/offline/warning/fault"),
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(Gateway)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Gateway.gateway_code.ilike(like)) | (Gateway.name.ilike(like)))
    if status:
        stmt = stmt.where(Gateway.status == status)
    stmt = stmt.offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.get("/{gateway_id}", response_model=GatewayOut)
def get_gateway(gateway_id: int, db: Session = Depends(get_db)):
    gateway = db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return gateway


@router.post("", response_model=GatewayOut, status_code=201)
def create_gateway(payload: GatewayCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.scalar(select(Gateway).where(Gateway.gateway_code == payload.gateway_code))
    if existing:
        raise HTTPException(status_code=409, detail="gateway_code already exists")
    gateway = Gateway(**payload.model_dump())
    db.add(gateway)
    db.commit()
    db.refresh(gateway)
    return gateway


@router.patch("/{gateway_id}", response_model=GatewayOut)
def update_gateway(gateway_id: int, payload: GatewayUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    gateway = db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(gateway, field, value)
    db.commit()
    db.refresh(gateway)
    return gateway


@router.delete("/{gateway_id}", status_code=204)
def delete_gateway(gateway_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """删除网关。被 TAG/设备引用时返回 409 并说明原因（与 devices.py 的
    删除保护一致，避免把正常的业务冲突抛成 500）。"""
    gateway = db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    try:
        db.delete(gateway)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=("该网关已被设备或测点引用，不能直接删除。请先在设备管理里把相关设备的"
                    "通讯配置改到其它网关后再删除。"),
        ) from None


@router.post("/{gateway_id}/test-connection", response_model=CommTestResult)
async def test_gateway_connection(
    gateway_id: int,
    timeout: float = Query(3.0, ge=0.5, le=15),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Test whether this gateway is actually reachable using its stored
    ip_address/port/protocol, and reflect the result in `status` +
    `last_heartbeat` so the gateway list isn't just a static, unverified flag.
    """
    gateway = db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    if not gateway.ip_address:
        raise HTTPException(status_code=400, detail="Gateway has no ip_address configured")

    outcome = await run_in_threadpool(
        comm_test.run_test, gateway.ip_address, gateway.port, gateway.protocol, timeout
    )

    gateway.status = "online" if outcome.success else "fault"
    if outcome.success:
        gateway.last_heartbeat = datetime.now(timezone.utc)
    db.commit()

    return CommTestResult(
        success=outcome.success,
        latency_ms=outcome.latency_ms,
        message=outcome.message,
        detail=outcome.detail,
    )


@router.get("/import/template")
def download_gateways_import_template():
    """Downloadable .xlsx template for 通讯管理 -> 批量导入."""
    content = table_import.build_excel_template(
        ["网关编号", "网关名称", "IP地址", "端口", "协议", "状态"],
        [["GW-001", "车间网关1", "192.168.1.1", "502", "MQTT", "online"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="网关导入模板.xlsx"),
    )


@router.post("/import", response_model=TableImportResult, status_code=201)
async def import_gateways(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import gateways from an uploaded CSV/Excel file (通讯管理 -> 批量导入).

    Auto-detects gateway_code/name/ip_address/port/protocol/status columns
    (English or Chinese headers) and upserts by gateway_code.
    """
    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename = upload.raw, upload.filename

    try:
        df = table_import.read_table(raw, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    columns_detected = table_import.detect_columns(df.columns, _IMPORT_SPEC)
    if "gateway_code" not in columns_detected or "name" not in columns_detected:
        raise HTTPException(
            status_code=400,
            detail="Could not detect required columns gateway_code/name (网关编号/网关名称)",
        )

    warnings: list[str] = []
    inserted = updated = skipped = 0

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            code = table_import.clean_str(table_import.row_value(row, columns_detected, "gateway_code"))
            name = table_import.clean_str(table_import.row_value(row, columns_detected, "name"))
            if not code or not name:
                skipped += 1
                warnings.append(f"Row {i + 2}: missing gateway_code or name — skipped")
                continue

            fields = dict(
                name=name,
                ip_address=table_import.clean_str(table_import.row_value(row, columns_detected, "ip_address")),
                port=table_import.clean_int(table_import.row_value(row, columns_detected, "port")),
                protocol=table_import.clean_str(table_import.row_value(row, columns_detected, "protocol")) or "MQTT",
                status=table_import.clean_str(table_import.row_value(row, columns_detected, "status")) or "offline",
            )

            existing = db.scalar(select(Gateway).where(Gateway.gateway_code == code))
            if existing:
                for k, v in fields.items():
                    if v is not None:
                        setattr(existing, k, v)
                updated += 1
            else:
                db.add(Gateway(gateway_code=code, **fields))
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
        updated=updated,
        skipped=skipped,
        status="done",
        warnings=warnings,
    )
