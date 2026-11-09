"""Devices API — CRUD for analyzer devices and their measured components."""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.device import Device, DeviceComponent, DeviceModel, Gateway
from app.models.system import User
from app.schemas.communication import CommTestResult
from app.schemas.device import DeviceCreate, DeviceOut, DeviceUpdate, TagConfigIn
from app.schemas.imports import TableImportResult
from app.services import comm_test, data_chain, table_import
from app.services.upload_security import read_upload

router = APIRouter(prefix="/devices", tags=["Devices"])

_IMPORT_SPEC = {
    "device_code": ("device_code", "code", "设备编号", "编号", "设备代码"),
    "name": ("device_name", "name", "设备名称", "名称"),
    "model_name": ("model", "model_name", "型号", "设备型号"),
    "protocol": ("protocol", "通讯协议", "协议"),
    "location": ("location", "位置", "安装位置"),
    "ip_address": ("ip_address", "ip", "ip地址"),
    "status": ("status", "状态"),
    "installed_at": ("installed_at", "install_date", "安装日期", "投用日期"),
    "gateway_code": ("gateway_code", "网关编号", "网关"),
}

_TAG_IMPORT_SPEC = {
    "device_code": ("device_code", "设备编号", "设备"),
    "component_name": ("tag_name", "component_name", "component", "tag", "组分", "标签", "参数", "位号"),
    "gateway_code": ("gateway_code", "网关编号", "网关"),
    "unit": ("unit", "单位"),
    "low_limit": ("low_limit", "下限", "低限"),
    "high_limit": ("high_limit", "上限", "高限"),
    "tag_address": ("tag_address", "address", "register", "地址", "寄存器"),
}


@router.get("", response_model=list[DeviceOut])
def list_devices(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by device code or name"),
    status: Optional[str] = Query(None, description="Filter by status: online/offline/warning/fault"),
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(Device)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Device.device_code.ilike(like)) | (Device.name.ilike(like)))
    if status:
        stmt = stmt.where(Device.status == status)
    stmt = stmt.offset(skip).limit(limit)
    return db.scalars(stmt).unique().all()


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.scalar(select(Device).where(Device.device_code == payload.device_code))
    if existing:
        raise HTTPException(status_code=409, detail="device_code already exists")
    device = Device(**payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.patch("/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, payload: DeviceUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, field, value)
    db.commit()
    db.refresh(device)
    return device


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()


@router.get("/{device_id}/components", response_model=list[dict])
def list_components(device_id: int, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    comps = db.scalars(select(DeviceComponent).where(DeviceComponent.device_id == device_id)).all()
    return [
        {
            "id": c.id,
            "component_name": c.component_name,
            "unit": c.unit,
            "is_major": c.is_major,
            "low_limit": float(c.low_limit) if c.low_limit is not None else None,
            "high_limit": float(c.high_limit) if c.high_limit is not None else None,
            "gateway_id": c.gateway_id,
            "gateway_code": c.gateway.gateway_code if c.gateway else None,
            "tag_address": c.tag_address,
        }
        for c in comps
    ]


@router.post("/{device_id}/components", response_model=dict, status_code=201)
def configure_component(
    device_id: int, payload: TagConfigIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """创建/更新一个 TAG 并绑定通讯(网关 + tag_address)。

    以前只能靠 CSV 批量导入(/tags/import)来配置 TAG 与网关的绑定; 这个接口是逐个配置的入口。
    同一网关下 tag_address 必须唯一 —— 否则网关推送(/communication/ingest)无法确定一个地址属于哪个 TAG。
    与批量导入不同, 这里是**显式配置**: 传入的字段会覆盖已有值。"""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    gateway = None
    if payload.gateway_code:
        gateway = db.scalar(select(Gateway).where(Gateway.gateway_code == payload.gateway_code))
        if gateway is None:
            raise HTTPException(status_code=404, detail=f"Gateway {payload.gateway_code} not found")
    address = (payload.tag_address or "").strip() or None
    if address and gateway is not None:
        clash = db.scalar(
            select(DeviceComponent).where(
                DeviceComponent.gateway_id == gateway.id,
                DeviceComponent.tag_address == address,
                ~((DeviceComponent.device_id == device_id) & (DeviceComponent.component_name == payload.component_name)),
            )
        )
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"网关 {gateway.gateway_code} 下地址 {address} 已被 TAG {clash.component_name}"
                       f"(设备 {clash.device_id}) 使用",
            )
    tag = data_chain.get_or_create_tag(db, device, payload.component_name, gateway=gateway)
    if payload.unit is not None:
        tag.unit = payload.unit
    if payload.low_limit is not None:
        tag.low_limit = payload.low_limit
    if payload.high_limit is not None:
        tag.high_limit = payload.high_limit
    if address is not None:
        tag.tag_address = address
    if gateway is not None:
        tag.gateway_id = gateway.id
        data_chain.link_gateway(db, device, gateway)
    tag.is_major = payload.is_major
    db.commit()
    db.refresh(tag)
    return {
        "id": tag.id, "component_name": tag.component_name, "unit": tag.unit, "is_major": tag.is_major,
        "low_limit": float(tag.low_limit) if tag.low_limit is not None else None,
        "high_limit": float(tag.high_limit) if tag.high_limit is not None else None,
        "gateway_id": tag.gateway_id, "gateway_code": tag.gateway.gateway_code if tag.gateway else None,
        "tag_address": tag.tag_address,
    }


@router.post("/{device_id}/test-connection", response_model=CommTestResult)
async def test_device_connection(
    device_id: int,
    port: Optional[int] = Query(None, description="覆盖默认端口（留空则按协议使用默认端口）"),
    timeout: float = Query(3.0, ge=0.5, le=15),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Test whether this device is actually reachable using its stored
    ip_address + protocol, and reflect the result in `status` so the device
    list doesn't keep showing a stale online/offline flag that no one has
    verified.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="Device has no ip_address configured")

    outcome = await run_in_threadpool(
        comm_test.run_test, device.ip_address, port, device.protocol, timeout
    )

    device.status = "online" if outcome.success else "fault"
    db.commit()

    return CommTestResult(
        success=outcome.success,
        latency_ms=outcome.latency_ms,
        message=outcome.message,
        detail=outcome.detail,
    )


@router.get("/import/template")
def download_devices_import_template():
    """Downloadable .xlsx template for 设备管理 -> 批量导入, with the exact
    column headers import_devices() auto-detects, plus one filled-in example
    row so it's obvious what belongs in each column."""
    content = table_import.build_excel_template(
        ["设备编号", "设备名称", "型号", "通讯协议", "位置", "IP地址", "状态", "安装日期", "网关编号"],
        [["DEV-001", "色谱仪示例", "GC-2000", "Modbus TCP", "车间一", "192.168.1.10", "online", "2024-01-01", "GW-001"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="设备导入模板.xlsx"),
    )


@router.post("/import", response_model=TableImportResult, status_code=201)
async def import_devices(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import devices from an uploaded CSV/Excel file (设备管理 -> 批量导入).

    Auto-detects columns for device_code/name/model/protocol/location/ip_address/
    status/installed_at (English or Chinese headers). Rows are upserted by
    device_code: an existing device is updated in place, a new device_code
    creates a new row. `model_name`, if present, resolves to (or creates) a
    DeviceModel by name so devices can be imported before models are set up.
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
    if "device_code" not in columns_detected or "name" not in columns_detected:
        raise HTTPException(
            status_code=400,
            detail="Could not detect required columns device_code/name (设备编号/设备名称)",
        )

    warnings: list[str] = []
    inserted = updated = skipped = 0
    model_cache: dict[str, int] = {}

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            code = table_import.clean_str(table_import.row_value(row, columns_detected, "device_code"))
            name = table_import.clean_str(table_import.row_value(row, columns_detected, "name"))
            if not code or not name:
                skipped += 1
                warnings.append(f"Row {i + 2}: missing device_code or name — skipped")
                continue

            model_id = None
            model_name = table_import.clean_str(table_import.row_value(row, columns_detected, "model_name"))
            if model_name:
                model_id = model_cache.get(model_name.lower())
                if model_id is None:
                    dm = db.scalar(select(DeviceModel).where(DeviceModel.model_name == model_name))
                    if dm is None:
                        dm = DeviceModel(model_name=model_name)
                        db.add(dm)
                        db.flush()
                    model_id = dm.id
                    model_cache[model_name.lower()] = model_id

            fields = dict(
                name=name,
                model_id=model_id,
                protocol=table_import.clean_str(table_import.row_value(row, columns_detected, "protocol")) or "Modbus TCP",
                location=table_import.clean_str(table_import.row_value(row, columns_detected, "location")),
                ip_address=table_import.clean_str(table_import.row_value(row, columns_detected, "ip_address")),
                status=table_import.clean_str(table_import.row_value(row, columns_detected, "status")) or "offline",
                installed_at=table_import.clean_date(table_import.row_value(row, columns_detected, "installed_at")),
            )

            existing = db.scalar(select(Device).where(Device.device_code == code))
            if existing:
                for k, v in fields.items():
                    if v is not None:
                        setattr(existing, k, v)
                device_row = existing
                updated += 1
            else:
                device_row = Device(device_code=code, **fields)
                db.add(device_row)
                db.flush()
                inserted += 1

            # 设备 -> 通讯: link to a gateway in the same row, if given
            gateway_code = table_import.clean_str(table_import.row_value(row, columns_detected, "gateway_code"))
            if gateway_code:
                gw = db.scalar(select(Gateway).where(Gateway.gateway_code == gateway_code))
                if gw is None:
                    warnings.append(f"Row {i + 2}: gateway_code '{gateway_code}' not found — device left unlinked")
                else:
                    data_chain.link_gateway(db, device_row, gw)

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


@router.get("/tags/import/template")
def download_tags_import_template():
    """Downloadable .xlsx template for 设备管理 -> TAG批量导入."""
    content = table_import.build_excel_template(
        ["设备编号", "位号", "网关编号", "单位", "下限", "上限", "地址"],
        [["DEV-001", "Methane", "GW-001", "%", "0", "100", "40001"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="TAG导入模板.xlsx"),
    )


@router.post("/tags/import", response_model=TableImportResult, status_code=201)
async def import_tags(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import TAGs (measured components) and wire the chain 设备 -> TAG ->
    通讯 in one step (设备管理 -> TAG批量导入).

    Auto-detects device_code/tag_name/gateway_code/unit/low_limit/high_limit/
    tag_address columns (English or Chinese headers). device_code must match
    an existing device (a tag always belongs to a device). gateway_code, if
    given and it matches an existing gateway, links the device to that
    gateway too — so importing tags is enough to wire up communication
    without a separate step. Upserts by (device_code, component_name).
    """
    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename = upload.raw, upload.filename

    try:
        df = table_import.read_table(raw, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    columns_detected = table_import.detect_columns(df.columns, _TAG_IMPORT_SPEC)
    if "device_code" not in columns_detected or "component_name" not in columns_detected:
        raise HTTPException(
            status_code=400,
            detail="Could not detect required columns device_code/tag_name (设备编号/位号)",
        )

    warnings: list[str] = []
    inserted = updated = skipped = 0
    device_cache: dict[str, Optional[Device]] = {}
    gateway_cache: dict[str, Optional[Gateway]] = {}

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            device_code = table_import.clean_str(table_import.row_value(row, columns_detected, "device_code"))
            component_name = table_import.clean_str(table_import.row_value(row, columns_detected, "component_name"))
            if not device_code or not component_name:
                skipped += 1
                warnings.append(f"Row {i + 2}: missing device_code or tag_name — skipped")
                continue

            if device_code not in device_cache:
                device_cache[device_code] = db.scalar(select(Device).where(Device.device_code == device_code))
            device = device_cache[device_code]
            if device is None:
                skipped += 1
                warnings.append(f"Row {i + 2}: device_code '{device_code}' not found — skipped (import devices first)")
                continue

            gateway = None
            gateway_code = table_import.clean_str(table_import.row_value(row, columns_detected, "gateway_code"))
            if gateway_code:
                if gateway_code not in gateway_cache:
                    gateway_cache[gateway_code] = db.scalar(select(Gateway).where(Gateway.gateway_code == gateway_code))
                gateway = gateway_cache[gateway_code]
                if gateway is None:
                    warnings.append(f"Row {i + 2}: gateway_code '{gateway_code}' not found — tag left unlinked to comms")

            was_existing = db.scalar(
                select(DeviceComponent).where(
                    DeviceComponent.device_id == device.id,
                    DeviceComponent.component_name == component_name,
                )
            )
            data_chain.get_or_create_tag(
                db,
                device,
                component_name,
                gateway=gateway,
                unit=table_import.clean_str(table_import.row_value(row, columns_detected, "unit")),
                low_limit=table_import.clean_number(table_import.row_value(row, columns_detected, "low_limit")),
                high_limit=table_import.clean_number(table_import.row_value(row, columns_detected, "high_limit")),
                tag_address=table_import.clean_str(table_import.row_value(row, columns_detected, "tag_address")),
            )
            if was_existing:
                updated += 1
            else:
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
