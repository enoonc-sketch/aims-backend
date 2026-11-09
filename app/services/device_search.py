"""DB layer for AI Center's cross-menu device search & 选型.

Assembles one "profile" per device from every menu that knows about it —
Devices (+ model), Comm (TAGs -> gateways), Data (measurements), Alarms,
Maintenance (work orders), Assets, Lab — with one grouped query per table
(no per-device queries), then hands the profiles to the pure ranking logic in
app/services/device_selection.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.alarm import Alarm
from app.models.device import Device, DeviceModel, Gateway, GatewayDevice
from app.models.maintenance import Asset, LabSample, MaintenanceRecord, WorkOrder
from app.models.measurement import Measurement
from app.services import device_selection as ds
from app.services import roi_analysis as roi
from app.services import studio_data

DATA_WINDOW_HOURS = 24
MIN_POINTS_24H = 4  # same default as health_weights.json -> min_expected_points_24h
ROI_WINDOW_MONTHS = 12  # trailing window used for the cost-health flags folded into selection scoring


def load_profiles(db: Session, device_ids: Optional[Iterable[int]] = None) -> list[dict]:
    """One profile dict per device (all devices when `device_ids` is None)."""
    ids_filter = list(device_ids) if device_ids is not None else None
    stmt = select(Device).options(selectinload(Device.components)).order_by(Device.device_code)
    if ids_filter is not None:
        if not ids_filter:
            return []
        stmt = stmt.where(Device.id.in_(ids_filter))
    devices = db.scalars(stmt).all()
    if not devices:
        return []

    def scope(stmt_, col):
        return stmt_.where(col.in_(ids_filter)) if ids_filter is not None else stmt_

    models = {m.id: m for m in db.scalars(select(DeviceModel)).all()}
    gw_codes = {gid: code for gid, code in db.execute(select(Gateway.id, Gateway.gateway_code)).all()}

    gw_by_dev: dict[int, set[str]] = {}
    for dev_id, gid in db.execute(scope(select(GatewayDevice.device_id, GatewayDevice.gateway_id), GatewayDevice.device_id)).all():
        if gid in gw_codes:
            gw_by_dev.setdefault(dev_id, set()).add(gw_codes[gid])

    alarms = dict(
        db.execute(
            scope(
                select(Alarm.device_id, func.count()).where(Alarm.status == "active").group_by(Alarm.device_id),
                Alarm.device_id,
            )
        ).all()
    )
    work_orders = dict(
        db.execute(
            scope(
                select(WorkOrder.device_id, func.count())
                .where(WorkOrder.status.in_(["open", "in_progress"]), WorkOrder.device_id.is_not(None))
                .group_by(WorkOrder.device_id),
                WorkOrder.device_id,
            )
        ).all()
    )
    lab = dict(
        db.execute(
            scope(
                select(LabSample.device_id, func.count()).where(LabSample.device_id.is_not(None)).group_by(LabSample.device_id),
                LabSample.device_id,
            )
        ).all()
    )

    since = datetime.now(timezone.utc) - timedelta(hours=DATA_WINDOW_HOURS)
    points: dict[int, int] = {}
    data_comps: dict[int, list[str]] = {}
    for dev_id, comp, n in db.execute(
        scope(
            select(Measurement.device_id, Measurement.component_name, func.count())
            .where(Measurement.time >= since)
            .group_by(Measurement.device_id, Measurement.component_name),
            Measurement.device_id,
        )
    ).all():
        points[dev_id] = points.get(dev_id, 0) + int(n)
        if comp:
            data_comps.setdefault(dev_id, []).append(comp)

    # A device can have more than one linked Asset row (e.g. a bare placeholder asset
    # created early on, plus a fully-filled-in one added later). Prefer whichever asset
    # actually carries a purchase_value — that's the one ROI can compute anything from —
    # instead of just the first row by id, which silently threw away cost-side evidence
    # whenever an earlier, value-less asset happened to exist for the same device.
    assets: dict[int, tuple] = {}
    for dev_id, code, name, value, dep_years, purchase_date in db.execute(
        scope(
            select(Asset.device_id, Asset.asset_code, Asset.name, Asset.purchase_value, Asset.depreciation_years, Asset.purchase_date)
            .where(Asset.device_id.is_not(None)).order_by(Asset.id),
            Asset.device_id,
        )
    ).all():
        current = assets.get(dev_id)
        if current is None or (current[2] is None and value is not None):
            assets[dev_id] = (code, name, value, dep_years, purchase_date)

    # Trailing-12-month evidence for the cost-health flags folded into selection scoring (see
    # device_selection._cost_signal). Grouped once for the whole fleet, same as everything above —
    # the per-device cost computation itself (roi_analysis.compute_roi) is pure Python, no further queries.
    roi_since = datetime.now(timezone.utc) - timedelta(days=30.4375 * ROI_WINDOW_MONTHS)
    maint_records: dict[int, list[dict]] = {}
    for dev_id, cost, performed_at in db.execute(
        scope(select(MaintenanceRecord.device_id, MaintenanceRecord.cost, MaintenanceRecord.performed_at).where(MaintenanceRecord.performed_at >= roi_since), MaintenanceRecord.device_id)
    ).all():
        maint_records.setdefault(dev_id, []).append({"cost": float(cost) if cost is not None else None, "performed_at": performed_at})
    wo_history: dict[int, list[dict]] = {}
    for dev_id, order_type, created_at in db.execute(
        scope(select(WorkOrder.device_id, WorkOrder.order_type, WorkOrder.created_at).where(WorkOrder.created_at >= roi_since, WorkOrder.device_id.is_not(None)), WorkOrder.device_id)
    ).all():
        wo_history.setdefault(dev_id, []).append({"order_type": order_type, "created_at": created_at})
    alarms_12m = dict(
        db.execute(scope(select(Alarm.device_id, func.count()).where(Alarm.triggered_at >= roi_since).group_by(Alarm.device_id), Alarm.device_id)).all()
    )

    # Latest AI-certification per device + component (app/services/lab_analysis.py, persisted in
    # lab_ai_certifications), folded into selection scoring (see device_selection._cert_signal).
    certifications = studio_data.latest_certifications_by_device(db, device_ids=ids_filter)

    profiles: list[dict] = []
    for d in devices:
        m = models.get(d.model_id) if d.model_id else None
        comps = []
        gws = set(gw_by_dev.get(d.id, set()))
        for c in d.components:
            g = gw_codes.get(c.gateway_id) if c.gateway_id else None
            if g:
                gws.add(g)
            comps.append({"name": c.component_name, "unit": c.unit, "gateway": g})
        asset = assets.get(d.id)
        asset_for_roi = None
        if asset and asset[2] is not None:  # has a purchase value -> cost-side ROI can be computed
            asset_for_roi = {"purchase_value": float(asset[2]), "depreciation_years": asset[3], "purchase_date": asset[4]}
        roi_flags: list[str] = []
        if asset_for_roi:
            roi_result = roi.compute_roi(asset_for_roi, maint_records.get(d.id, []), wo_history.get(d.id, []), int(alarms_12m.get(d.id, 0)), ROI_WINDOW_MONTHS)
            roi_flags = [f["code"] for f in roi_result["flags"]]
        profiles.append(
            {
                "id": d.id,
                "device_code": d.device_code,
                "name": d.name,
                "status": d.status or "offline",
                "health_score": float(d.health_score) if d.health_score is not None else 0.0,
                "protocol": d.protocol,
                "location": d.location,
                "ip_address": d.ip_address,
                "model_name": m.model_name if m else None,
                "manufacturer": m.manufacturer if m else None,
                "analyzer_type": m.analyzer_type if m else None,
                "components": comps,
                "components_with_data": data_comps.get(d.id, []),
                "gateways": sorted(gws),
                "asset_code": asset[0] if asset else None,
                "asset_name": asset[1] if asset else None,
                "active_alarms": int(alarms.get(d.id, 0)),
                "open_work_orders": int(work_orders.get(d.id, 0)),
                "points_24h": points.get(d.id, 0),
                "lab_samples": int(lab.get(d.id, 0)),
                "certifications": certifications.get(d.id, {}),
                "roi_flags": roi_flags,
            }
        )
    return profiles


# ---------------------------------------------------------------- 搜索 ----
def search_devices(
    db: Session,
    q: Optional[str] = None,
    *,
    status: Optional[str] = None,
    analyzer_type: Optional[str] = None,
    component: Optional[str] = None,
    protocol: Optional[str] = None,
    limit: int = 20,
) -> dict:
    parsed = ds.merge_filters(
        ds.parse_query(q),
        status=status,
        analyzer_type=analyzer_type,
        component=component,
        protocol=protocol,
    )
    total, hits = ds.search(load_profiles(db), parsed, limit)
    return {"query": q or "", "parsed": ds.describe_parsed(parsed), "total": total, "items": hits}


# ---------------------------------------------------------------- 选型 ----
def select_devices(
    db: Session,
    *,
    query: Optional[str] = None,
    components: Optional[list[str]] = None,
    analyzer_type: Optional[str] = None,
    protocol: Optional[str] = None,
    online_only: bool = False,
    min_health: float = 0.0,
    location: Optional[str] = None,
    limit: int = 5,
) -> dict:
    req = ds.build_requirement(
        query,
        components=components,
        analyzer_type=analyzer_type,
        protocol=protocol,
        online_only=online_only,
        min_health=min_health,
        location=location,
    )
    profiles = load_profiles(db)
    considered, ranked = ds.rank_devices(profiles, req, limit=limit, min_points=MIN_POINTS_24H)
    devices = []
    for r in ranked:
        hit = ds.to_hit(r["profile"], r["score"])
        hit.update(fit=r["fit"], missing_components=r["missing_components"], reasons=r["reasons"])
        devices.append(hit)
    return {
        "requirement": ds.describe_requirement(req),
        "total_devices": len(profiles),
        "considered": considered,
        "devices": devices,
        "models": ds.rank_models(profiles, req, limit=limit),
        "basis_zh": ds.MODEL_BASIS["zh"],
        "basis_en": ds.MODEL_BASIS["en"],
    }


def select_devices_raw(db: Session, query: str, limit: int = 5) -> tuple[dict, int, list[dict], list[dict]]:
    """Same ranking as select_devices() but keeps the raw profile dicts —
    used by chat to render a text block for the LLM."""
    req = ds.build_requirement(query)
    profiles = load_profiles(db)
    considered, ranked = ds.rank_devices(profiles, req, limit=limit, min_points=MIN_POINTS_24H)
    return req, considered, ranked, ds.rank_models(profiles, req, limit=3)


# ------------------------------------------------------- 360 / facets ------
def device_context(db: Session, device_id: int) -> Optional[dict]:
    profiles = load_profiles(db, [device_id])
    if not profiles:
        return None
    p = profiles[0]
    return {"device": ds.to_hit(p), "links": ds.build_links(p)}


def device_facets(db: Session) -> dict:
    return ds.build_facets(load_profiles(db))
