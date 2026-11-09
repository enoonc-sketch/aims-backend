"""Data Analysis API — statistical cores, multi-trend, saved datasets.

Corresponds to the prototype "Data Analysis" (数据分析) page.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.analysis import DataAnalysisDataset
from app.models.device import Device, DeviceComponent
from app.models.measurement import Measurement, MeasurementImport
from app.models.system import User
from app.schemas.analysis import (
    AnalysisCoresRequest,
    AnalysisCoresResponse,
    DatasetCreate,
    DatasetOut,
    MultiTrendRequest,
    MultiTrendResponse,
    MultiTrendSeries,
    SeriesStats,
)
from app.services import table_import
from app.services.upload_security import check_dataframe, csv_row_cap, read_upload

router = APIRouter(prefix="/analysis", tags=["Data Analysis"])


def _stats(values: list[float], name: str) -> SeriesStats:
    if not values:
        return SeriesStats(component_name=name, count=0)
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n if n else 0.0
    return SeriesStats(
        component_name=name,
        count=n,
        min=min(values),
        max=max(values),
        mean=round(mean, 6),
        std=round(math.sqrt(var), 6),
        sum=round(sum(values), 6),
        last=values[-1],
    )


@router.post("/cores", response_model=AnalysisCoresResponse)
def compute_cores(payload: AnalysisCoresRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Compute statistical cores (min/max/mean/std/sum) for analyzer components.

    Used by the Data Analysis page for KPI cards and component-sum checks.
    """
    if not db.get(Device, payload.device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    components = payload.components
    if not components:
        components = list(
            db.scalars(
                select(DeviceComponent.component_name).where(
                    DeviceComponent.device_id == payload.device_id
                )
            ).all()
        )
    if not components:
        return AnalysisCoresResponse(
            device_id=payload.device_id, hours=payload.hours, series=[], sample_count=0
        )

    since = datetime.now(timezone.utc) - timedelta(hours=payload.hours)
    series_out: list[SeriesStats] = []
    # time -> {comp: value} for optional sum
    by_time: dict[datetime, dict[str, float]] = {}

    for comp in components:
        rows = db.scalars(
            select(Measurement)
            .where(
                Measurement.device_id == payload.device_id,
                Measurement.component_name == comp,
                Measurement.time >= since,
            )
            .order_by(Measurement.time.asc())
        ).all()
        values = [float(r.value) for r in rows]
        series_out.append(_stats(values, comp))
        for r in rows:
            by_time.setdefault(r.time, {})[comp] = float(r.value)

    component_sum_stats = None
    if payload.include_sum and by_time:
        sums = []
        for t in sorted(by_time.keys()):
            vals = by_time[t]
            # only sum times that have at least one of the requested components
            present = [vals[c] for c in components if c in vals]
            if present:
                sums.append(sum(present))
        component_sum_stats = _stats(sums, "ComponentSum")

    sample_count = max((s.count for s in series_out), default=0)
    return AnalysisCoresResponse(
        device_id=payload.device_id,
        hours=payload.hours,
        series=series_out,
        component_sum=component_sum_stats,
        sample_count=sample_count,
    )


@router.post("/multi-trend", response_model=MultiTrendResponse)
def multi_trend(payload: MultiTrendRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Return multi-series time-series for charting (Data Analysis curves)."""
    if not db.get(Device, payload.device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    if not payload.components:
        raise HTTPException(status_code=400, detail="components required")

    since = datetime.now(timezone.utc) - timedelta(hours=payload.hours)
    series_list: list[MultiTrendSeries] = []

    for comp in payload.components:
        rows = db.scalars(
            select(Measurement)
            .where(
                Measurement.device_id == payload.device_id,
                Measurement.component_name == comp,
                Measurement.time >= since,
            )
            .order_by(Measurement.time.asc())
        ).all()
        # downsample if needed
        step = max(1, len(rows) // payload.max_points) if rows else 1
        points = [
            {"time": r.time.isoformat(), "value": float(r.value)}
            for i, r in enumerate(rows)
            if i % step == 0
        ]
        series_list.append(MultiTrendSeries(component_name=comp, points=points))

    return MultiTrendResponse(
        device_id=payload.device_id, hours=payload.hours, series=series_list
    )


@router.get("/components/{device_id}", response_model=list[str])
def list_analysis_components(device_id: int, db: Session = Depends(get_db)):
    """Component names available for a device (Y-axis checklist)."""
    if not db.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    names = db.scalars(
        select(DeviceComponent.component_name)
        .where(DeviceComponent.device_id == device_id)
        .order_by(DeviceComponent.component_name)
    ).all()
    if names:
        return list(names)
    # fallback from measurements
    from sqlalchemy import distinct

    return list(
        db.scalars(
            select(distinct(Measurement.component_name)).where(
                Measurement.device_id == device_id
            )
        ).all()
    )


# ---------- Bulk import (auto-creates a ready-to-chart dataset) ----------
@router.get("/import/template")
def download_analysis_import_template():
    """Downloadable .xlsx template for 数据分析 -> 批量导入到服务器.

    Unlike the other modules this one is free-form: the first column is a
    timestamp and every other column is a component/tag name of your
    choosing — whatever columns you keep, that's what gets charted. This
    template just shows the expected shape with two example components.
    """
    content = table_import.build_excel_template(
        ["时间", "组分1", "组分2"],
        [
            ["2026-09-19 00:00:00", "40.1", "85.2"],
            ["2026-09-19 01:00:00", "40.5", "85.0"],
        ],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="数据分析导入模板.xlsx"),
    )


@router.post("/import", response_model=DatasetOut, status_code=201)
async def import_analysis_file(
    device_id: int = Form(...),
    file: UploadFile = File(...),
    name: Optional[str] = Form(None, description="Dataset name; defaults to the file name"),
    time_column: Optional[str] = Form(None, description="Override auto-detected timestamp column name"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Import a CSV/Excel file straight into Data Analysis (数据分析 -> 批量导入).

    Reuses the same column auto-detection as Data -> 数据导入 (measurements
    import): one timestamp column plus either per-component numeric columns
    ("wide") or explicit Component/Value columns ("long"). On top of that,
    this endpoint immediately saves a DataAnalysisDataset pointing at the
    resulting import + detected columns, so the file shows up ready to chart
    on this page without a separate "create dataset" step.
    """
    from app.api.v1.measurements import _dataframe_to_measurements
    import io

    if not db.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename, ext = upload.raw, upload.filename, upload.ext

    import pandas as pd

    try:
        df = pd.read_csv(io.BytesIO(raw), nrows=csv_row_cap()) if ext == "csv" else pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")
    check_dataframe(df)
    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    import_row = MeasurementImport(
        file_name=filename, device_id=device_id, row_count=len(df), column_count=len(df.columns), status="processing"
    )
    db.add(import_row)
    db.commit()
    db.refresh(import_row)

    try:
        rows, columns_detected, used_time_col, fmt, warnings = _dataframe_to_measurements(
            df, device_id, import_row.id, time_column
        )
    except ValueError as e:
        import_row.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))

    if not rows:
        import_row.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail="No valid measurement rows found after parsing")

    try:
        db.bulk_save_objects(rows)
        from app.services import data_chain

        device_row = db.get(Device, device_id)
        seen_components: set[str] = set()
        for r in rows:
            if r.component_name not in seen_components:
                data_chain.get_or_create_tag(db, device_row, r.component_name)
                seen_components.add(r.component_name)
            data_chain.evaluate_alarm_rules(db, device_row, r.component_name, r.value)
        import_row.status = "done"
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import_row.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail=f"Import failed while saving measurements: {e}")

    dataset = DataAnalysisDataset(
        name=name or filename,
        source_import_id=import_row.id,
        x_field=used_time_col,
        y_fields=columns_detected if fmt == "wide" else None,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


# ---------- Saved analysis datasets ----------
@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db), skip: int = SkipQuery, limit: int = LimitQuery(50)):
    return db.scalars(
        select(DataAnalysisDataset)
        .order_by(DataAnalysisDataset.id.desc())
        .offset(skip)
        .limit(limit)
    ).all()


@router.post("/datasets", response_model=DatasetOut, status_code=201)
def create_dataset(payload: DatasetCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = DataAnalysisDataset(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    row = db.get(DataAnalysisDataset, dataset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return row


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(DataAnalysisDataset, dataset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")
    db.delete(row)
    db.commit()


@router.get("/cores-info")
def cores_info():
    """List open-source analysis cores bundled with this build."""
    import json
    from pathlib import Path
    from app.main import CORES_DIR
    if CORES_DIR is None:
        return {"available": False, "cores": []}
    index = CORES_DIR / "index.json"
    if index.is_file():
        data = json.loads(index.read_text(encoding="utf-8"))
        return {"available": True, "vendor": str(CORES_DIR), **data}
    files = [p.name for p in CORES_DIR.glob("*.js")]
    return {"available": True, "vendor": str(CORES_DIR), "cores": [{"filename": f, "local_path": f"/cores/{f}"} for f in files]}
