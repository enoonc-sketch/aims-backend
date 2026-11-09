"""DB layer for the AI-generated SOP / ROI, Data Analysis reading and data-analysis-driven
Lab comparison. All the maths lives in the pure modules (roi_analysis, analysis_reading,
lab_analysis, ai_generate); this file only loads their inputs and stores their outputs."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis
from app.models.alarm import Alarm
from app.models.device import Device, DeviceComponent, DeviceModel
from app.models.maintenance import (
    Asset,
    LabAICertification,
    LabCorrelationResult,
    LabSample,
    MaintenanceRecord,
    MaintenanceSOP,
    SparePart,
    WorkOrder,
)
from app.models.measurement import Measurement
from app.services import lab_analysis as la
from app.services import roi_analysis as roi

logger = logging.getLogger(__name__)
AI_DRAFT_VERSION = "AI-draft"
_EXCERPT = 240


def excerpt(text: Optional[str], n: int = _EXCERPT) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ------------------------------------------------------------ knowledge ----
async def kb_search(db: Session, query: str, limit: int = 3) -> list[dict]:
    """Top knowledge-base chunks for `query` (embedding similarity when a model is available,
    else bag-of-words). Never raises — a broken index must not break the feature using it."""
    from app.services.rag import embed_texts, search_chunks

    try:
        vecs = await embed_texts([query], db=db)
        hits = search_chunks(db, query, limit=limit, query_embedding=vecs[0] if vecs else None)
    except Exception:  # noqa: BLE001
        logger.exception("knowledge search failed")
        return []
    return [
        {"chunk_id": c.id, "document_id": c.document_id, "document_title": title,
         "excerpt": excerpt(c.content), "score": round(float(score), 4)}
        for c, title, score in hits
        if score and float(score) > 0
    ]


# ------------------------------------------------------------------ SOP ----
def sop_context(db: Session, device: Device, sop_type: str, component: Optional[str], kb: list[dict]) -> dict:
    model = db.get(DeviceModel, device.model_id) if device.model_id else None
    base = None
    if device.model_id:
        # a vetted SOP only — never build on a previous AI draft
        base = db.scalar(
            select(MaintenanceSOP)
            .where(MaintenanceSOP.device_model_id == device.model_id, MaintenanceSOP.sop_type == sop_type,
                   (MaintenanceSOP.version.is_(None)) | (MaintenanceSOP.version != AI_DRAFT_VERSION))
            .order_by(MaintenanceSOP.id)
        )
    alarms = db.scalars(
        select(Alarm).where(Alarm.device_id == device.id, Alarm.status == "active").order_by(Alarm.triggered_at.desc()).limit(5)
    ).all()
    wos = db.scalars(
        select(WorkOrder).where(WorkOrder.device_id == device.id, WorkOrder.status.in_(["open", "in_progress"])).order_by(WorkOrder.id.desc()).limit(5)
    ).all()
    dq = select(AIDiagnosis).where(AIDiagnosis.device_id == device.id)
    if component:
        dq = dq.where(AIDiagnosis.component_name == component)
    diag = db.scalar(dq.order_by(AIDiagnosis.id.desc()))
    parts = []
    if device.model_id:
        parts = db.scalars(select(SparePart).where(SparePart.device_model_id == device.model_id).order_by(SparePart.part_code).limit(8)).all()
    corr = None
    if component:
        corr = db.scalar(
            select(LabCorrelationResult).where(LabCorrelationResult.device_id == device.id, LabCorrelationResult.component_name == component)
            .order_by(LabCorrelationResult.id.desc())
        )
    return {
        "device": {"code": device.device_code, "name": device.name, "model": model.model_name if model else None,
                   "manufacturer": model.manufacturer if model else None, "analyzer_type": model.analyzer_type if model else None,
                   "location": device.location},
        "sop_type": sop_type,
        "component": component,
        "base_sop": {"id": base.id, "title": base.title, "content": base.content, "version": base.version} if base else None,
        "open_alarms": [{"severity": a.severity, "component": a.component_name, "message": a.message} for a in alarms],
        "open_work_orders": [{"id": w.id, "title": w.title, "order_type": w.order_type, "priority": w.priority} for w in wos],
        "last_diagnosis": {"component": diag.component_name, "diagnosis": excerpt(diag.diagnosis, 300)} if diag else None,
        "spare_parts": [{"part_code": p.part_code, "name": p.name, "stock_qty": int(p.stock_qty or 0)} for p in parts],
        "correlation": {"r_squared": float(corr.r_squared), "mean_deviation": float(corr.mean_deviation)}
        if corr and corr.r_squared is not None and corr.mean_deviation is not None else None,
        "kb": [{"title": k["document_title"], "excerpt": k["excerpt"], "score": k["score"]} for k in kb],
    }


# ------------------------------------------------------------------ ROI ----
def roi_inputs(db: Session, device: Device, period_months: int) -> tuple[Optional[dict], list[dict], list[dict], int]:
    since = datetime.now(timezone.utc) - timedelta(days=roi.DAYS_PER_MONTH * period_months)
    asset = db.scalar(select(Asset).where(Asset.device_id == device.id).order_by(Asset.id))
    records = db.scalars(
        select(MaintenanceRecord).where(MaintenanceRecord.device_id == device.id, MaintenanceRecord.performed_at >= since)
    ).all()
    wos = db.scalars(select(WorkOrder).where(WorkOrder.device_id == device.id, WorkOrder.created_at >= since)).all()
    alarms = db.scalar(select(func.count()).select_from(Alarm).where(Alarm.device_id == device.id, Alarm.triggered_at >= since)) or 0
    asset_d = None
    if asset:
        asset_d = {"asset_code": asset.asset_code, "name": asset.name,
                   "purchase_value": float(asset.purchase_value) if asset.purchase_value is not None else None,
                   "depreciation_years": asset.depreciation_years, "purchase_date": asset.purchase_date}
    return (
        asset_d,
        [{"cost": float(r.cost) if r.cost is not None else None, "performed_at": r.performed_at} for r in records],
        [{"order_type": w.order_type, "status": w.status, "created_at": w.created_at} for w in wos],
        int(alarms),
    )


# --------------------------------------------------------- data analysis ----
def device_component_names(db: Session, device_id: int, limit: int = 40) -> list[str]:
    return list(db.scalars(select(DeviceComponent.component_name).where(DeviceComponent.device_id == device_id).limit(limit)).all())


def series_from_db(db: Session, device_id: int, components: list[str], hours: int, max_points: int = 2000) -> dict[str, list[Optional[float]]]:
    """Aligned series (one value per distinct timestamp, None where a component has no reading)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(
        select(Measurement.time, Measurement.component_name, Measurement.value)
        .where(Measurement.device_id == device_id, Measurement.component_name.in_(components), Measurement.time >= since)
        .order_by(Measurement.time.asc())
    ).all()
    by_time: dict[Any, dict[str, float]] = {}
    for t, comp, val in rows:
        by_time.setdefault(t, {})[comp] = float(val)
    times = sorted(by_time)
    step = max(1, len(times) // max_points)
    times = times[::step]
    return {c: [by_time[t].get(c) for t in times] for c in components if any(c in by_time[t] for t in times)}


# ------------------------------------------------------------------ Lab ----
def lab_compare(db: Session, device_id: int, component: str, window_minutes: float, aggregate: str,
                date_from: Optional[datetime], date_to: Optional[datetime]) -> dict:
    """Pair every lab sample with the analyzer's measurement data around its sampling time."""
    q = select(LabSample).where(LabSample.device_id == device_id, LabSample.component_name == component)
    if date_from:
        q = q.where(LabSample.sampled_at >= date_from)
    if date_to:
        q = q.where(LabSample.sampled_at <= date_to)
    rows = db.scalars(q.order_by(LabSample.sampled_at.asc())).all()
    samples = [{"id": s.id, "sample_no": s.sample_no, "sampled_at": s.sampled_at,
                "lab_value": float(s.lab_value) if s.lab_value is not None else None} for s in rows]
    times: list[float] = []
    values: list[float] = []
    stamped = [s["sampled_at"] for s in samples if s["sampled_at"] is not None and s["lab_value"] is not None]
    if stamped:
        w = timedelta(minutes=window_minutes)
        lo, hi = min(stamped) - w, max(stamped) + w
        m = db.execute(
            select(Measurement.time, Measurement.value)
            .where(Measurement.device_id == device_id, Measurement.component_name == component, Measurement.time >= lo, Measurement.time <= hi)
            .order_by(Measurement.time.asc())
        ).all()
        times = [la._epoch(t) for t, _ in m]
        values = [float(v) for _, v in m]
    pairs, unmatched = la.pair_with_measurements(samples, times, values, window_minutes=window_minutes, aggregate=aggregate)
    with_lab = sum(1 for s in samples if s["lab_value"] is not None)
    return {"sample_count": with_lab, "pairs": pairs, "unmatched": unmatched, "stats": la.compare_stats(pairs)}


def latest_certification_ids(db: Session, device_id: Optional[int] = None, component_name: Optional[str] = None) -> list[int]:
    """id of the newest certification per (device_id, component_name), portable across SQLite and
    Postgres (a MAX(id)-per-group subquery rather than Postgres-only DISTINCT ON)."""
    q = select(func.max(LabAICertification.id))
    if device_id is not None:
        q = q.where(LabAICertification.device_id == device_id)
    if component_name:
        q = q.where(LabAICertification.component_name == component_name)
    q = q.group_by(LabAICertification.device_id, LabAICertification.component_name)
    return list(db.scalars(q).all())


def latest_certifications_by_device(db: Session, device_ids: Optional[list[int]] = None) -> dict[int, dict[str, dict]]:
    """{device_id: {canonical_component_key: {status, score, valid_until, certified_at}}} — the newest
    certification per component, for every device (or just `device_ids`). Used to fold certification
    evidence into AI-selection scoring and the AI-Center cross-menu links."""
    from app.services.device_selection import canon_component

    ids = latest_certification_ids(db)  # all devices: cheap, and the caller usually wants most of the fleet
    if not ids:
        return {}
    rows = db.scalars(select(LabAICertification).where(LabAICertification.id.in_(ids))).all()
    now = datetime.now(timezone.utc)
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        if device_ids is not None and r.device_id not in device_ids:
            continue
        vu = r.valid_until
        if vu is not None and vu.tzinfo is None:
            vu = vu.replace(tzinfo=timezone.utc)
        out.setdefault(r.device_id, {})[canon_component(r.component_name)] = {
            "component_name": r.component_name, "status": r.status, "score": float(r.score) if r.score is not None else None,
            "valid_until": vu.isoformat() if vu else None, "expired": bool(vu is not None and vu < now), "certified_at": r.certified_at.isoformat() if r.certified_at else None,
        }
    return out


def upsert_correlation(db: Session, device_id: int, component: str, pairs: list[dict], stats: dict) -> Optional[LabCorrelationResult]:
    """Keep the existing Lab correlation table in step: same fields, but derived from analysis data."""
    if stats.get("pairs", 0) < 2:
        return None
    slope, intercept = stats["slope"], stats["intercept"]
    mean_dev = sum(abs(p["online"] - (slope * p["lab"] + intercept)) for p in pairs) / len(pairs)
    row = db.scalar(select(LabCorrelationResult).where(LabCorrelationResult.device_id == device_id, LabCorrelationResult.component_name == component))
    if row is None:
        row = LabCorrelationResult(device_id=device_id, component_name=component)
        db.add(row)
    row.sample_count = stats["pairs"]
    row.r_squared = round(stats["r_squared"], 4)
    row.slope = round(slope, 6)
    row.intercept = round(intercept, 6)
    row.mean_deviation = round(mean_dev, 6)
    return row
