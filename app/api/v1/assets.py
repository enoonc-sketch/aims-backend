"""Assets API — asset register, document attachments, ROI / depreciation analysis.

Corresponds to the prototype's "Assets" page (asset table + upload) and the
Maintenance > ROI Analysis tab (device + period -> depreciation/ROI figures).
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.device import Device
from app.models.maintenance import Asset, AssetDocument
from app.models.system import User
from app.schemas.asset import (
    AssetCreate,
    AssetDocumentCreate,
    AssetDocumentOut,
    AssetOut,
    AssetROIResult,
    AssetUpdate,
)
from app.schemas.imports import TableImportResult
from app.services import table_import
from app.services.upload_security import read_upload

router = APIRouter(prefix="/assets", tags=["Assets"])

_IMPORT_SPEC = {
    "asset_code": ("asset_code", "code", "资产编号", "编号", "资产代码"),
    "name": ("asset_name", "name", "资产名称", "名称"),
    "category": ("category", "类别", "分类"),
    "device_code": ("device_code", "关联设备", "设备编号"),
    "purchase_date": ("purchase_date", "采购日期", "购入日期"),
    "purchase_value": ("purchase_value", "采购金额", "原值", "购入价格"),
    "depreciation_years": ("depreciation_years", "折旧年限"),
    "status": ("status", "状态"),
}


@router.get("", response_model=list[AssetOut])
def list_assets(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by asset code or name"),
    status: Optional[str] = Query(None, description="in_use/idle/scrapped"),
    device_id: Optional[int] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(Asset)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Asset.asset_code.ilike(like)) | (Asset.name.ilike(like)))
    if status:
        stmt = stmt.where(Asset.status == status)
    if device_id:
        stmt = stmt.where(Asset.device_id == device_id)
    stmt = stmt.offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.get("/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.post("", response_model=AssetOut, status_code=201)
def create_asset(payload: AssetCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.scalar(select(Asset).where(Asset.asset_code == payload.asset_code))
    if existing:
        raise HTTPException(status_code=409, detail="asset_code already exists")
    if payload.device_id and not db.get(Device, payload.device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    data = payload.model_dump()
    # initialize current_value to purchase_value if not otherwise known
    asset = Asset(**data, current_value=payload.purchase_value)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@router.patch("/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)
    db.commit()
    db.refresh(asset)
    return asset


@router.delete("/{asset_id}", status_code=204)
def delete_asset(asset_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(asset)
    db.commit()


# ============================================================
# ROI / depreciation analysis (Maintenance > ROI tab in the prototype)
# ============================================================
def _compute_roi(asset: Asset, period_months: int) -> AssetROIResult:
    purchase_value = float(asset.purchase_value or 0)
    depreciation_years = asset.depreciation_years or 10
    total_months = max(depreciation_years * 12, 1)

    monthly_dep = purchase_value / total_months
    accumulated = min(monthly_dep * period_months, purchase_value)
    net_book_value = purchase_value - accumulated
    depreciation_pct = (accumulated / purchase_value * 100) if purchase_value else 0.0

    return AssetROIResult(
        asset_id=asset.id,
        period_months=period_months,
        purchase_value=purchase_value,
        current_value=net_book_value,
        straight_line_monthly_depreciation=monthly_dep,
        accumulated_depreciation=accumulated,
        net_book_value=net_book_value,
        depreciation_pct=depreciation_pct,
    )


@router.get("/{asset_id}/roi", response_model=AssetROIResult)
def get_asset_roi(
    asset_id: int,
    db: Session = Depends(get_db),
    period_months: int = Query(12, ge=1, le=600),
    persist: bool = Query(True, description="Write the computed net book value back to current_value"),
    user: User = Depends(get_current_user),
):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    result = _compute_roi(asset, period_months)
    if persist:
        asset.current_value = result.net_book_value
        db.commit()
    return result


@router.get("/by-device/{device_id}/roi", response_model=AssetROIResult)
def get_asset_roi_by_device(
    device_id: int,
    db: Session = Depends(get_db),
    period_months: int = Query(12, ge=1, le=600),
):
    """Convenience lookup matching the prototype's device-driven ROI selector."""
    asset = db.scalar(select(Asset).where(Asset.device_id == device_id))
    if not asset:
        raise HTTPException(status_code=404, detail="No asset is linked to this device")
    return _compute_roi(asset, period_months)


# ============================================================
# Asset documents
# ============================================================
@router.get("/{asset_id}/documents", response_model=list[AssetDocumentOut])
def list_asset_documents(asset_id: int, db: Session = Depends(get_db)):
    if not db.get(Asset, asset_id):
        raise HTTPException(status_code=404, detail="Asset not found")
    return db.scalars(select(AssetDocument).where(AssetDocument.asset_id == asset_id)).all()


@router.post("/{asset_id}/documents", response_model=AssetDocumentOut, status_code=201)
def add_asset_document(asset_id: int, payload: AssetDocumentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not db.get(Asset, asset_id):
        raise HTTPException(status_code=404, detail="Asset not found")
    doc = AssetDocument(asset_id=asset_id, **payload.model_dump())
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


# ============================================================
# Bulk import
# ============================================================
@router.get("/import/template")
def download_assets_import_template():
    """Downloadable .xlsx template for 资产管理 -> 批量导入."""
    content = table_import.build_excel_template(
        ["资产编号", "资产名称", "类别", "设备编号", "采购日期", "采购金额", "折旧年限", "状态"],
        [["AST-001", "在线分析仪主机", "分析设备", "DEV-001", "2024-01-01", "150000", "10", "in_use"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="资产导入模板.xlsx"),
    )


@router.post("/import", response_model=TableImportResult, status_code=201)
async def import_assets(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import assets from an uploaded CSV/Excel file (资产管理 -> 批量导入).

    Auto-detects asset_code/name/category/device_code/purchase_date/
    purchase_value/depreciation_years/status columns (English or Chinese
    headers). `device_code`, if present, links the asset to an existing
    device. Rows are upserted by asset_code.
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
    if "asset_code" not in columns_detected or "name" not in columns_detected:
        raise HTTPException(
            status_code=400,
            detail="Could not detect required columns asset_code/name (资产编号/资产名称)",
        )

    warnings: list[str] = []
    inserted = updated = skipped = 0
    device_cache: dict[str, Optional[int]] = {}

    try:
        for i in range(len(df)):
            row = df.iloc[i]
            code = table_import.clean_str(table_import.row_value(row, columns_detected, "asset_code"))
            name = table_import.clean_str(table_import.row_value(row, columns_detected, "name"))
            if not code or not name:
                skipped += 1
                warnings.append(f"Row {i + 2}: missing asset_code or name — skipped")
                continue

            device_id = None
            device_code = table_import.clean_str(table_import.row_value(row, columns_detected, "device_code"))
            if device_code:
                if device_code not in device_cache:
                    dev = db.scalar(select(Device).where(Device.device_code == device_code))
                    device_cache[device_code] = dev.id if dev else None
                    if dev is None:
                        warnings.append(f"Row {i + 2}: device_code '{device_code}' not found — asset left unlinked")
                device_id = device_cache[device_code]

            purchase_value = table_import.clean_number(
                table_import.row_value(row, columns_detected, "purchase_value")
            )
            fields = dict(
                name=name,
                category=table_import.clean_str(table_import.row_value(row, columns_detected, "category")),
                device_id=device_id,
                purchase_date=table_import.clean_date(table_import.row_value(row, columns_detected, "purchase_date")),
                purchase_value=purchase_value,
                depreciation_years=table_import.clean_int(
                    table_import.row_value(row, columns_detected, "depreciation_years")
                ) or 10,
                status=table_import.clean_str(table_import.row_value(row, columns_detected, "status")) or "in_use",
            )

            existing = db.scalar(select(Asset).where(Asset.asset_code == code))
            if existing:
                for k, v in fields.items():
                    if v is not None:
                        setattr(existing, k, v)
                updated += 1
            else:
                db.add(Asset(asset_code=code, current_value=purchase_value, **fields))
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
