"""Lab API — lab samples & correlation results (Lab comparison page)."""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.device import Device
from app.models.maintenance import LabCorrelationResult, LabSample
from app.models.system import User
from app.schemas.imports import TableImportResult
from app.schemas.lab import (
    LabCorrelationOut,
    LabCorrelationRequest,
    LabSampleCreate,
    LabSampleOut,
)
from app.services import table_import
from app.services import data_chain, data_health
from app.services.upload_security import read_upload

router = APIRouter(prefix="/lab", tags=["Lab"])

_IMPORT_SPEC = {
    "device_code": ("device_code", "设备编号", "设备"),
    "component_name": ("component_name", "component", "组分", "参数"),
    "sample_no": ("sample_no", "样品编号", "编号"),
    "lab_value": ("lab_value", "化验值", "实验室值", "lab"),
    "online_value": ("online_value", "在线值", "online"),
    "sampled_at": ("sampled_at", "time", "采样时间", "日期"),
}


@router.get("/samples", response_model=list[LabSampleOut])
def list_samples(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    component_name: Optional[str] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(LabSample)
    if device_id is not None:
        stmt = stmt.where(LabSample.device_id == device_id)
    if component_name:
        stmt = stmt.where(LabSample.component_name == component_name)
    stmt = stmt.order_by(LabSample.id.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/samples", response_model=LabSampleOut, status_code=201)
def create_sample(payload: LabSampleCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = LabSample(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/samples/{sample_id}", status_code=204)
def delete_sample(sample_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(LabSample, sample_id)
    if not row:
        raise HTTPException(status_code=404, detail="Sample not found")
    db.delete(row)
    db.commit()


@router.get("/samples/import/template")
def download_samples_import_template():
    """Downloadable .xlsx template for 实验室 -> 批量导入."""
    content = table_import.build_excel_template(
        ["设备编号", "组分", "样品编号", "化验值", "在线值", "采样时间"],
        [["DEV-001", "Methane", "S-0001", "85.1", "85.4", "2026-09-19 08:00:00"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="实验室导入模板.xlsx"),
    )


@router.post("/samples/import", response_model=TableImportResult, status_code=201)
async def import_samples(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import lab-vs-online paired samples from an uploaded CSV/Excel
    file (实验室 -> 批量导入). Auto-detects device_code/component_name/
    sample_no/lab_value/online_value/sampled_at columns (English or Chinese
    headers). Append-only, like the measurements importer — run
    /lab/correlations/compute afterwards to refresh the correlation fit.
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
    if "lab_value" not in columns_detected and "online_value" not in columns_detected:
        raise HTTPException(
            status_code=400,
            detail="Could not detect lab_value or online_value columns (化验值/在线值)",
        )

    warnings: list[str] = []
    inserted = skipped = 0
    device_cache: dict[str, Optional[int]] = {}

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            lab_value = table_import.clean_number(table_import.row_value(row, columns_detected, "lab_value"))
            online_value = table_import.clean_number(table_import.row_value(row, columns_detected, "online_value"))
            if lab_value is None and online_value is None:
                skipped += 1
                warnings.append(f"Row {i + 2}: no lab_value or online_value — skipped")
                continue

            device_id = None
            device_code = table_import.clean_str(table_import.row_value(row, columns_detected, "device_code"))
            if device_code:
                if device_code not in device_cache:
                    dev = db.scalar(select(Device).where(Device.device_code == device_code))
                    device_cache[device_code] = dev.id if dev else None
                    if dev is None:
                        warnings.append(f"Row {i + 2}: device_code '{device_code}' not found — sample left unlinked")
                device_id = device_cache[device_code]

            sampled_at = table_import.clean_datetime(table_import.row_value(row, columns_detected, "sampled_at"))
            kwargs = dict(
                device_id=device_id,
                component_name=table_import.clean_str(table_import.row_value(row, columns_detected, "component_name")),
                sample_no=table_import.clean_str(table_import.row_value(row, columns_detected, "sample_no")),
                lab_value=lab_value,
                online_value=online_value,
            )
            if sampled_at is not None:
                kwargs["sampled_at"] = sampled_at
            db.add(LabSample(**kwargs))
            inserted += 1

            # 实验室 -> TAG: make sure the sampled component has a TAG on this
            # device, so lab-vs-online comparison and the trend page use the
            # same identity as everything else.
            if device_cache.get(device_code) and kwargs["component_name"]:
                data_chain.get_or_create_tag(db, db.get(Device, device_id), kwargs["component_name"])

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


@router.get("/correlations", response_model=list[LabCorrelationOut])
def list_correlations(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
):
    stmt = select(LabCorrelationResult)
    if device_id is not None:
        stmt = stmt.where(LabCorrelationResult.device_id == device_id)
    return db.scalars(stmt).all()


@router.post("/correlations/compute", response_model=LabCorrelationOut)
def compute_correlation(payload: LabCorrelationRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Simple linear regression between lab_value (x) and online_value (y)."""
    samples = db.scalars(
        select(LabSample).where(
            LabSample.device_id == payload.device_id,
            LabSample.component_name == payload.component_name,
            LabSample.lab_value.isnot(None),
            LabSample.online_value.isnot(None),
        )
    ).all()
    if len(samples) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 paired samples")

    xs = [float(s.lab_value) for s in samples]
    ys = [float(s.online_value) for s in samples]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_yy = sum((y - mean_y) ** 2 for y in ys)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = ss_xy / ss_xx if ss_xx else 0.0
    intercept = mean_y - slope * mean_x
    r_sq = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx and ss_yy else 0.0
    mean_dev = sum(abs(y - (slope * x + intercept)) for x, y in zip(xs, ys)) / n

    existing = db.scalar(
        select(LabCorrelationResult).where(
            LabCorrelationResult.device_id == payload.device_id,
            LabCorrelationResult.component_name == payload.component_name,
        )
    )
    if existing:
        existing.sample_count = n
        existing.r_squared = round(r_sq, 4)
        existing.slope = round(slope, 6)
        existing.intercept = round(intercept, 6)
        existing.mean_deviation = round(mean_dev, 6)
        row = existing
    else:
        row = LabCorrelationResult(
            device_id=payload.device_id,
            component_name=payload.component_name,
            sample_count=n,
            r_squared=round(r_sq, 4),
            slope=round(slope, 6),
            intercept=round(intercept, 6),
            mean_deviation=round(mean_dev, 6),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/data-coverage")
def lab_data_coverage(
    device_id: int,
    component_name: str,
    window_minutes: float = Query(30.0, gt=0, le=1440),
    db: Session = Depends(get_db),
):
    """化验样品 × 在线数据覆盖：每条样品采样时刻前后窗口内有没有在线数据，
    没有的话指出是哪个数据断点（外部数据源同步产生的）造成的。"""
    return data_health.lab_sample_coverage(
        db, device_id, component_name, window_minutes=window_minutes
    )
