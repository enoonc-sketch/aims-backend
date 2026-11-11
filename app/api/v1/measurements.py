"""Measurements API — ingest and query time-series analyzer data (TimescaleDB)."""
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.device import Device
from app.models.external_source import ExternalDatabase
from app.models.measurement import Measurement, MeasurementImport
from app.models.system import User
from app.schemas.measurement import (
    HistoryPage,
    HistoryRow,
    ImportBatchOut,
    ImportResult,
    MeasurementBatchIn,
    MeasurementIn,
    MeasurementOut,
    TrendPoint,
    TrendSeries,
)
from app.services import data_chain
from app.services.upload_security import check_dataframe, csv_row_cap, read_upload

router = APIRouter(prefix="/measurements", tags=["Measurements"])
logger = logging.getLogger("aims.measurements")

_MAX_IMPORT_BYTES = 20 * 1024 * 1024  # 20 MB
_TIME_COLUMN_CANDIDATES = ("time", "timestamp", "date", "datetime", "time(s)", "时间", "采集时间", "日期")
_COMPONENT_COLUMN_CANDIDATES = ("component", "component_name", "tag", "组分", "参数", "测点")
_VALUE_COLUMN_CANDIDATES = ("value", "reading", "数值", "读数", "测量值")

# Look-back window bound for /measurements and /measurements/trend. 720h (30 days) was
# too tight for legitimate historical queries (e.g. reviewing a year of imported data)
# and made the endpoint reject a plain "last N days" request from callers who import
# months of backfilled data at once. `limit` is what actually bounds result size, so
# the time window itself just needs a generous sanity cap rather than a tight one.
_MAX_LOOKBACK_HOURS = 24 * 3650  # ~10 years


def _chain_summary(out: data_chain.ChainOutcome) -> dict:
    return {
        "quality": out.quality,
        "alarms_created": len(out.alarms),
        "data_quality_alarms_created": len(out.dq_alarms),
        "work_orders_created": len(out.work_orders),
    }


@router.post("", response_model=MeasurementOut, status_code=201)
def ingest_measurement(payload: MeasurementIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    row = Measurement(
        time=payload.time or datetime.now(timezone.utc),
        device_id=payload.device_id,
        component_name=payload.component_name,
        value=payload.value,
        quality=payload.quality,
    )
    # 数据质量评估 -> 写库 -> TAG -> 报警 -> AI诊断 -> 知识库 -> 工单 (row.quality 会被就地定级)
    data_chain.process_readings(db, device, [row], realtime=payload.time is None)
    db.commit()
    return row


@router.post("/batch", status_code=201)
def ingest_batch(payload: MeasurementBatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-insert readings for a device, e.g. from a CSV/Excel import (Data page)."""
    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    rows = [
        Measurement(
            time=r.time or datetime.now(timezone.utc),
            device_id=payload.device_id,
            component_name=r.component_name,
            value=r.value,
            quality=r.quality,
        )
        for r in payload.readings
    ]
    out = data_chain.process_readings(db, device, rows, realtime=False)
    db.commit()
    return {"inserted": len(rows), **_chain_summary(out)}


def _pick_column(columns, candidates: tuple[str, ...]) -> Optional[str]:
    lower_map = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def _utc_naive_array(times):
    """把（可能带时区的）pandas 时间列转成 datetime64[ns] 的 UTC 朴素数组。

    tz-aware 的 DatetimeIndex.to_numpy() 会返回 object 数组（元素是 Timestamp，
    带 tz），再传 tz= 给 Timestamp 就会报错；先统一到 UTC 再去掉时区最稳。
    """
    if getattr(times.dt, "tz", None) is not None:
        times = times.dt.tz_convert("UTC").dt.tz_localize(None)
    return times.to_numpy(dtype="datetime64[ns]")


def _dataframe_to_measurements(df, device_id: int, import_id: int, time_column: Optional[str] = None):
    """Turn an uploaded CSV/Excel DataFrame into Measurement rows.

    Supports two common layouts from the Data-import page:
      - "wide": one timestamp column + one numeric column per analyzer component
        (e.g. Time, Nitrogen, Methane, Ethane, ...)
      - "long": explicit component/value columns, one reading per row
        (e.g. Time, Component, Value)

    Returns (rows, columns_detected, time_col_name, fmt, warnings).
    """
    import pandas as pd

    if df.empty or len(df.columns) == 0:
        raise ValueError("File has no columns")

    time_col = time_column if time_column in df.columns else None
    if time_col is None:
        time_col = _pick_column(df.columns, _TIME_COLUMN_CANDIDATES)
    if time_col is None:
        time_col = df.columns[0]  # fall back: assume the first column is the timestamp

    times = pd.to_datetime(df[time_col], errors="coerce", utc=True)
    if times.isna().all():
        raise ValueError(
            f"Could not parse any timestamps from column '{time_col}'. "
            "Include a Time/Timestamp/时间 column, or pass time_column explicitly."
        )

    warnings: list[str] = []
    n_bad_time = int(times.isna().sum())
    if n_bad_time:
        warnings.append(f"{n_bad_time} row(s) had an unparseable timestamp and were skipped")

    comp_col = _pick_column(df.columns, _COMPONENT_COLUMN_CANDIDATES)
    value_col = _pick_column(df.columns, _VALUE_COLUMN_CANDIDATES)

    rows: list[Measurement] = []

    if comp_col is not None and value_col is not None:
        fmt = "long"
        columns_detected = [comp_col, value_col]
        values = pd.to_numeric(df[value_col], errors="coerce")
        n_bad_value = int(values.isna().sum())
        if n_bad_value:
            warnings.append(f"{n_bad_value} row(s) had a non-numeric value and were skipped")
        # 同 wide 分支：用 numpy 数组按下标遍历，避免 pandas 标量访问开销
        time_values = _utc_naive_array(times)
        comp_values = df[comp_col].to_numpy(dtype=object)
        val_values = values.to_numpy(dtype="float64", na_value=float("nan"))
        for i in range(len(time_values)):
            if pd.isna(time_values[i]) or val_values[i] != val_values[i]:
                continue
            comp = comp_values[i]
            comp_name = "" if comp is None else str(comp).strip()
            if not comp_name or comp_name.lower() == "nan":
                continue
            rows.append(
                Measurement(
                    time=pd.Timestamp(time_values[i]).replace(tzinfo=timezone.utc),
                    device_id=device_id,
                    component_name=comp_name,
                    value=float(val_values[i]),
                    quality="good",
                    import_id=import_id,
                )
            )
    else:
        fmt = "wide"
        value_cols = [c for c in df.columns if c != time_col and pd.api.types.is_numeric_dtype(df[c])]
        if not value_cols:
            # last resort: try to coerce every non-time column to numeric
            for c in df.columns:
                if c == time_col:
                    continue
                coerced = pd.to_numeric(df[c], errors="coerce")
                if coerced.notna().any():
                    df[c] = coerced
                    value_cols.append(c)
        if not value_cols:
            raise ValueError(
                "No numeric measurement columns detected. Expected either one numeric "
                "column per component, or Component/Value columns."
            )
        columns_detected = [str(c) for c in value_cols]
        # 性能：原来每格用 df[c].iloc[i] + times.iloc[i] 取标量，pandas 的标量访问
        # 每次都有路由开销，2 万行 × N 列就是十万次，实测把导入压到 3.7k 行/秒。
        # 转成 numpy 数组按下标取值（同一语义），时间戳每行只转换一次而不是每列一次。
        time_values = _utc_naive_array(times)   # datetime64[ns]，UTC 已归一
        col_values = {c: df[c].to_numpy(dtype="float64", na_value=float("nan")) for c in value_cols}
        for i in range(len(time_values)):
            if pd.isna(time_values[i]):
                continue
            ts = pd.Timestamp(time_values[i]).replace(tzinfo=timezone.utc)
            for c in value_cols:
                v = col_values[c][i]
                if v != v:  # NaN
                    continue
                rows.append(
                    Measurement(
                        time=ts,
                        device_id=device_id,
                        component_name=str(c).strip(),
                        value=float(v),
                        quality="good",
                        import_id=import_id,
                    )
                )

    return rows, columns_detected, str(time_col), fmt, warnings


@router.post("/import", response_model=ImportResult, status_code=201)
async def import_measurements_file(
    device_id: int = Form(...),
    file: UploadFile = File(...),
    time_column: Optional[str] = Form(None, description="Override auto-detected timestamp column name"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Import measurements from an uploaded CSV/Excel file (Data page 数据导入).

    Auto-detects the timestamp column plus either per-component numeric columns
    ("wide" layout) or explicit Component/Value columns ("long" layout), and bulk
    -inserts the readings so they immediately feed AI Center diagnosis/prediction/
    health-score, which all read straight from the `measurements` table.
    """
    if not db.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename, ext = upload.raw, upload.filename, upload.ext

    import pandas as pd

    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(raw), nrows=csv_row_cap())
        else:
            df = pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")
    check_dataframe(df)

    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    import_row = MeasurementImport(
        file_name=filename,
        device_id=device_id,
        row_count=len(df),
        column_count=len(df.columns),
        status="processing",
        source="local",
        uploaded_by=user.id,
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

    device = db.get(Device, device_id)
    try:
        out = data_chain.process_readings(db, device, rows, realtime=False)
    except IntegrityError as exc:
        # 写入层已做幂等 upsert；这里兜的是其它约束（如 import_id 外键、历史库
        # 缺少首列主键等），绝不能再让用户看到 500 + 堆栈。
        db.rollback()
        logger.warning("measurement import failed (constraint): %s", exc)
        try:
            failed = db.get(MeasurementImport, import_row.id)
            if failed is not None:
                failed.status = "failed"
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        raise HTTPException(
            status_code=409,
            detail="导入的数据与已有记录冲突，本次导入已回滚，数据库未被修改。"
                   "请检查文件中的时间戳与测点名称后重试。",
        ) from exc
    if out.quality.get("bad") or out.quality.get("uncertain"):
        warnings.append(
            f"数据质量: good {out.quality['good']} / uncertain {out.quality['uncertain']} / bad {out.quality['bad']}"
        )
    import_row.status = "done"
    db.commit()

    logger.info(
        "Imported %d measurement rows for device=%s from %s (%s layout, columns=%s)",
        len(rows), device_id, filename, fmt, columns_detected,
    )

    return ImportResult(
        import_id=import_row.id,
        file_name=filename,
        device_id=device_id,
        row_count=len(df),
        rows_inserted=len(rows),
        columns_detected=columns_detected,
        time_column=used_time_col,
        format=fmt,
        status="done",
        warnings=warnings,
    )


@router.get("", response_model=list[MeasurementOut])
def query_measurements(
    db: Session = Depends(get_db),
    device_id: int = Query(...),
    component_name: Optional[str] = None,
    hours: int = Query(24, ge=1, le=_MAX_LOOKBACK_HOURS, description="Look-back window in hours"),
    limit: int = Query(2000, le=10000),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(Measurement).where(Measurement.device_id == device_id, Measurement.time >= since)
    if component_name:
        stmt = stmt.where(Measurement.component_name == component_name)
    stmt = stmt.order_by(Measurement.time.asc()).limit(limit)
    return db.scalars(stmt).all()


@router.get("/trend", response_model=list[TrendSeries])
def get_trend(
    db: Session = Depends(get_db),
    device_id: int = Query(...),
    components: list[str] = Query(..., description="One or more component names, e.g. Nitrogen"),
    hours: int = Query(24, ge=1, le=_MAX_LOOKBACK_HOURS),
    # Perf opt (report item 7, 2026-11-09): this endpoint previously had no
    # row cap at all — at 1 sample/second, 24h is 86,400 rows per component.
    # Default 5,000 points per component bounds the response; raise it
    # explicitly (max 20,000) if a caller really needs full resolution.
    limit: int = Query(5000, ge=1, le=20000, description="Max points returned per component"),
):
    """Used by the Data > Trend page and Data Analysis curve rendering."""
    if not db.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    series: list[TrendSeries] = []
    for comp in components:
        # Newest `limit` points, returned in chronological order: the window
        # can hold far more rows than the cap, and the trend page cares about
        # the recent curve, not the oldest slice of the window.
        rows = db.scalars(
            select(Measurement)
            .where(
                Measurement.device_id == device_id,
                Measurement.component_name == comp,
                Measurement.time >= since,
            )
            .order_by(Measurement.time.desc())
            .limit(limit)
        ).all()
        rows = list(reversed(rows))
        series.append(
            TrendSeries(
                component_name=comp,
                points=[TrendPoint(time=r.time, value=r.value) for r in rows],
            )
        )
    return series


def _source_of(imp: Optional[MeasurementImport]) -> str:
    return imp.source if imp is not None and imp.source else "local"


@router.get("/components", response_model=list[str])
def list_components(
    db: Session = Depends(get_db),
    device_id: int = Query(...),
    source: Optional[str] = Query(None, pattern="^(local|cloud)$"),
):
    """Distinct component names that have data for a device — fills the Data > Trend
    "curve field" dropdown from real data (including data synced from 云数据)."""
    stmt = select(Measurement.component_name).where(Measurement.device_id == device_id)
    if source:
        stmt = stmt.join(MeasurementImport, Measurement.import_id == MeasurementImport.id)
        stmt = stmt.where(MeasurementImport.source == source)
    return sorted(db.scalars(stmt.distinct()).all())


@router.get("/history", response_model=HistoryPage)
def measurement_history(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    component_name: Optional[str] = None,
    source: Optional[str] = Query(None, pattern="^(local|cloud)$", description="local=手动导入, cloud=云数据同步"),
    external_database_id: Optional[int] = Query(None, description="只看某个云数据库同步进来的数据"),
    import_id: Optional[int] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Data > 历史数据: paged raw readings with their origin. Rows written by a cloud
    sync (云数据) carry the cloud database name; rows from uploads carry the file name.
    Rows without an import batch (realtime ingest / older data) count as local."""
    conds = []
    if device_id is not None:
        conds.append(Measurement.device_id == device_id)
    if component_name:
        conds.append(Measurement.component_name == component_name)
    if start is not None:
        conds.append(Measurement.time >= start)
    if end is not None:
        conds.append(Measurement.time <= end)
    if import_id is not None:
        conds.append(Measurement.import_id == import_id)
    if external_database_id is not None:
        conds.append(MeasurementImport.external_database_id == external_database_id)
    if source == "cloud":
        conds.append(MeasurementImport.source == "cloud")
    elif source == "local":
        conds.append((MeasurementImport.id.is_(None)) | (MeasurementImport.source != "cloud"))

    base = (
        select(Measurement, MeasurementImport, Device.device_code, ExternalDatabase.name)
        .outerjoin(MeasurementImport, Measurement.import_id == MeasurementImport.id)
        .outerjoin(ExternalDatabase, MeasurementImport.external_database_id == ExternalDatabase.id)
        .outerjoin(Device, Measurement.device_id == Device.id)
    )
    count_stmt = (
        select(func.count())
        .select_from(Measurement)
        .outerjoin(MeasurementImport, Measurement.import_id == MeasurementImport.id)
    )
    for c in conds:
        base = base.where(c)
        count_stmt = count_stmt.where(c)
    total = db.scalar(count_stmt) or 0
    rows = db.execute(base.order_by(Measurement.time.desc()).limit(limit).offset(offset)).all()
    items = [
        HistoryRow(
            time=m.time,
            device_id=m.device_id,
            device_code=code,
            component_name=m.component_name,
            value=m.value,
            quality=m.quality,
            source=_source_of(imp),
            import_id=m.import_id,
            source_name=(db_name if imp is not None and imp.source == "cloud" else (imp.file_name if imp is not None else None)),
        )
        for m, imp, code, db_name in rows
    ]
    return HistoryPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/imports", response_model=list[ImportBatchOut])
def list_import_batches(
    db: Session = Depends(get_db),
    source: Optional[str] = Query(None, pattern="^(local|cloud)$"),
    limit: int = Query(30, ge=1, le=200),
):
    """Data > 导入记录: manual uploads and 云数据 syncs together, newest first."""
    stmt = (
        select(MeasurementImport, Device.device_code, ExternalDatabase.name)
        .outerjoin(Device, MeasurementImport.device_id == Device.id)
        .outerjoin(ExternalDatabase, MeasurementImport.external_database_id == ExternalDatabase.id)
    )
    if source:
        stmt = stmt.where(MeasurementImport.source == source)
    stmt = stmt.order_by(MeasurementImport.id.desc()).limit(limit)
    result = db.execute(stmt).all()
    ids = [imp.id for imp, _, _ in result]
    stored: dict[int, int] = {}
    if ids:
        for iid, n in db.execute(
            select(Measurement.import_id, func.count()).where(Measurement.import_id.in_(ids)).group_by(Measurement.import_id)
        ).all():
            stored[iid] = n
    return [
        ImportBatchOut(
            id=imp.id,
            file_name=imp.file_name,
            device_id=imp.device_id,
            device_code=code,
            row_count=imp.row_count,
            column_count=imp.column_count,
            status=imp.status,
            source=imp.source or "local",
            external_database_id=imp.external_database_id,
            external_database_name=db_name,
            rows_stored=stored.get(imp.id, 0),
            created_at=imp.created_at,
        )
        for imp, code, db_name in result
    ]
