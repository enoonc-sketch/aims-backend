"""AI Center — cross-menu device search, 选型 (selection) and 360° device context.

These endpoints are what bind AI Center to the other menus: they read the same
Devices / Comm / Data / Alarms / Maintenance / Assets / Lab tables those menus
own, so the device picker in AI Center, the recommendations, and the chat
assistant all agree with what the rest of the platform shows.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.system import User
from app.schemas.device_search import (
    DeviceContextOut,
    DeviceFacets,
    DeviceSearchResponse,
    DeviceSelectRequest,
    DeviceSelectResponse,
)
from app.services import device_search

router = APIRouter(prefix="/ai/devices", tags=["AI Center · Device Search & Selection"])


@router.get("/search", response_model=DeviceSearchResponse)
def search_devices(
    q: str = Query("", description="Free text: code / name / model / location / TAG, or natural language such as '在线 GC H2S'"),
    status: Optional[str] = Query(None, description="online / offline / warning / fault"),
    analyzer_type: Optional[str] = Query(None, description="e.g. GC, FTIR"),
    component: Optional[str] = Query(None, description="Required TAG/component; aliases understood (H2S = Hydrogen Sulfide). Separate several with ';'"),
    protocol: Optional[str] = Query(None, description="e.g. Modbus TCP, OPC UA"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Search devices across every menu's data. Status / component / analyzer type /
    protocol words inside `q` are recognised (EN and 中文) and applied as filters;
    the response's `parsed` field shows what was understood."""
    return device_search.search_devices(
        db, q, status=status, analyzer_type=analyzer_type, component=component, protocol=protocol, limit=limit
    )


@router.post("/select", response_model=DeviceSelectResponse)
def select_devices(payload: DeviceSelectRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """选型: rank existing devices — and the device models behind them — against
    a requirement (components to measure, analyzer type, protocol, online only...).
    Every candidate comes with bilingual reasons and any missing components."""
    return device_search.select_devices(
        db,
        query=payload.query,
        components=payload.components,
        analyzer_type=payload.analyzer_type,
        protocol=payload.protocol,
        online_only=payload.online_only,
        min_health=payload.min_health,
        location=payload.location,
        limit=payload.limit,
    )


@router.get("/facets", response_model=DeviceFacets)
def device_facets(db: Session = Depends(get_db)):
    """Distinct components / analyzer types / protocols / statuses / models on the
    platform — feeds the selection form's suggestions."""
    return device_search.device_facets(db)


@router.get("/{device_id}/context", response_model=DeviceContextOut)
def device_context(device_id: int, db: Session = Depends(get_db)):
    """The device's footprint in every other menu (TAGs/gateways, data, alarms,
    work orders, asset, lab) — drives the AI Center's cross-menu jump panel."""
    ctx = device_search.device_context(db, device_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return ctx
