"""External Data Source API — 数据管理：配置厂商云平台账号密码、站内十几个
数据库的抓取方式，手动/定时刷新下载并导入 measurements，查看抓取日志和数据
连续性巡检结果。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.external_source import (
    DataContinuityGap,
    ExternalDatabase,
    ExternalSource,
    ExternalSyncLog,
)
from app.models.system import User
from app.schemas.external import (
    DataContinuityGapOut,
    ExternalDatabaseCreate,
    ExternalDatabaseOut,
    ExternalDatabaseUpdate,
    ExternalSourceCreate,
    ExternalSourceOut,
    ExternalSourceUpdate,
    ExternalSyncLogOut,
    ExternalSyncResult,
)
from app.services import data_health, external_scheduler
from app.services.crypto_util import encrypt_secret
from app.services.external_scraper import ScraperError, external_session
from app.services.external_sync_service import run_sync_for_database

router = APIRouter(prefix="/external", tags=["External Data Source"])


# ============================================================== Sources ===
@router.get("/sources", response_model=list[ExternalSourceOut])
def list_sources(db: Session = Depends(get_db), skip: int = SkipQuery, limit: int = LimitQuery(100)):
    stmt = select(ExternalSource).order_by(ExternalSource.id.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/sources", response_model=ExternalSourceOut, status_code=201)
def create_source(payload: ExternalSourceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = payload.model_dump(exclude={"password"})
    row = ExternalSource(**data, password_encrypted=encrypt_secret(payload.password))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/sources/{source_id}", response_model=ExternalSourceOut)
def get_source(source_id: int, db: Session = Depends(get_db)):
    row = db.get(ExternalSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return row


@router.put("/sources/{source_id}", response_model=ExternalSourceOut)
def update_source(
    source_id: int,
    payload: ExternalSourceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(ExternalSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    data = payload.model_dump(exclude_unset=True, exclude={"password"})
    for field, value in data.items():
        setattr(row, field, value)
    if payload.password:
        row.password_encrypted = encrypt_secret(payload.password)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(ExternalSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    for database in list(row.databases):
        external_scheduler.unschedule_database(database.id)
    db.delete(row)
    db.commit()
    return None


@router.post("/sources/{source_id}/test-login")
def test_login(source_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Only logs in (no download) — use this after filling in a source's
    selectors to check they actually match the real login page, before
    attaching any databases to it."""
    source = db.get(ExternalSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        with external_session(source):
            pass
    except ScraperError as e:
        source.status = "login_failed"
        source.last_error = str(e)
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))
    from datetime import datetime, timezone

    source.status = "ok"
    source.last_login_at = datetime.now(timezone.utc)
    source.last_error = None
    db.commit()
    return {"status": "ok", "message": f"登录「{source.name}」成功"}


# ============================================================ Databases ===
@router.get("/databases", response_model=list[ExternalDatabaseOut])
def list_databases(
    db: Session = Depends(get_db),
    source_id: Optional[int] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(ExternalDatabase)
    if source_id is not None:
        stmt = stmt.where(ExternalDatabase.source_id == source_id)
    stmt = stmt.order_by(ExternalDatabase.id.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/databases", response_model=ExternalDatabaseOut, status_code=201)
def create_database(
    payload: ExternalDatabaseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(ExternalSource, payload.source_id) is None:
        raise HTTPException(status_code=404, detail="Source not found")
    row = ExternalDatabase(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    external_scheduler.schedule_database(row)
    return row


@router.get("/databases/{database_id}", response_model=ExternalDatabaseOut)
def get_database(database_id: int, db: Session = Depends(get_db)):
    row = db.get(ExternalDatabase, database_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Database not found")
    return row


@router.put("/databases/{database_id}", response_model=ExternalDatabaseOut)
def update_database(
    database_id: int,
    payload: ExternalDatabaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(ExternalDatabase, database_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Database not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    external_scheduler.schedule_database(row)  # re-schedule in case interval/enabled changed
    return row


@router.delete("/databases/{database_id}", status_code=204)
def delete_database(database_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(ExternalDatabase, database_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Database not found")
    external_scheduler.unschedule_database(database_id)
    db.delete(row)
    db.commit()
    return None


@router.post("/databases/{database_id}/refresh", response_model=ExternalSyncResult)
def refresh_database(database_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """手动"刷新"按钮：同步执行一次登录+下载+导入+连续性巡检，完成后直接返回
    结果（不是后台任务）——目标站点较慢时这个请求可能要等数十秒。"""
    database = db.get(ExternalDatabase, database_id)
    if database is None:
        raise HTTPException(status_code=404, detail="Database not found")
    log = run_sync_for_database(db, database, triggered_by="manual")
    return ExternalSyncResult(
        log=ExternalSyncLogOut.model_validate(log),
        rows_downloaded=log.rows_downloaded,
        rows_imported=log.rows_imported,
        gaps_found=log.gaps_found,
        status=log.status,
        error_message=log.error_message,
    )


@router.get("/databases/{database_id}/logs", response_model=list[ExternalSyncLogOut])
def list_logs(database_id: int, db: Session = Depends(get_db), skip: int = SkipQuery, limit: int = LimitQuery(50)):
    stmt = (
        select(ExternalSyncLog)
        .where(ExternalSyncLog.database_id == database_id)
        .order_by(ExternalSyncLog.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return db.scalars(stmt).all()


@router.get("/databases/{database_id}/continuity", response_model=list[DataContinuityGapOut])
def list_continuity_gaps_for_database(
    database_id: int, db: Session = Depends(get_db), skip: int = SkipQuery, limit: int = LimitQuery(100)
):
    stmt = (
        select(DataContinuityGap)
        .where(DataContinuityGap.database_id == database_id)
        .order_by(DataContinuityGap.gap_start.desc())
        .offset(skip)
        .limit(limit)
    )
    return db.scalars(stmt).all()


# ================================================================ Gaps ====
@router.get("/gaps", response_model=list[DataContinuityGapOut])
def list_continuity_gaps(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    resolved: Optional[bool] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    """跨所有数据库的数据连续性断点总览，用来评估设备的数据可维护性。"""
    stmt = select(DataContinuityGap)
    if device_id is not None:
        stmt = stmt.where(DataContinuityGap.device_id == device_id)
    if resolved is not None:
        stmt = stmt.where(DataContinuityGap.resolved == resolved)
    stmt = stmt.order_by(DataContinuityGap.gap_start.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/gaps/{gap_id}/resolve", response_model=DataContinuityGapOut)
def resolve_gap(gap_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    gap = db.get(DataContinuityGap, gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail="Gap not found")
    gap.resolved = True
    db.commit()
    db.refresh(gap)
    return gap


# ============================================================= Health =====
@router.get("/databases/{database_id}/health")
def database_health(database_id: int, db: Session = Depends(get_db)):
    """抓取可维护性：成功率、连续失败次数、最近成功时间、未处理断点数。"""
    database = db.get(ExternalDatabase, database_id)
    if database is None:
        raise HTTPException(status_code=404, detail="Database not found")
    return data_health.database_health(db, database)


@router.get("/coverage")
def data_coverage(
    device_id: int,
    component_name: str,
    hours: int = Query(168, ge=1, le=24 * 365),
    expected_interval_minutes: int = Query(60, ge=1),
    db: Session = Depends(get_db),
):
    """某设备某组分的数据覆盖率/断点数/最长断点/是否已经停更。"""
    return data_health.coverage_summary(
        db, device_id, component_name,
        expected_interval_minutes=expected_interval_minutes, hours=hours,
    )
