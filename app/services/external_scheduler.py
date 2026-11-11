"""APScheduler wiring for ExternalDatabase.auto_refresh_enabled /
refresh_interval_minutes — the "自动下载/定时刷新" half of the external-data
module. The "手动刷新" half just calls
`app.services.external_sync_service.run_sync_for_database` directly from
the API endpoint.

Runs in-process (APScheduler BackgroundScheduler): it only fires while this
FastAPI server process is alive, which matches this backend's normal
single-instance deployment. Running multiple API replicas of this service
would double-schedule jobs unless this is moved to a dedicated worker
process reading the same `is_enabled`/`auto_refresh_enabled` config.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.external_source import ExternalDatabase

logger = logging.getLogger("aims.external_scheduler")

_scheduler = None  # type: ignore[var-annotated]


def _job_id(database_id: int) -> str:
    return f"external_sync_{database_id}"


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            logger.warning("APScheduler is not installed; external auto-refresh is disabled")
            return None
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.start()
    return _scheduler


def _run_job(database_id: int) -> None:
    from app.services.external_sync_service import run_sync_for_database

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        database = db.get(ExternalDatabase, database_id)
        if database is None or not database.is_enabled or not database.auto_refresh_enabled:
            return
        run_sync_for_database(db, database, triggered_by="scheduled")
    except Exception:
        logger.exception("Scheduled sync failed for external_database_id=%s", database_id)
    finally:
        db.close()


def schedule_database(database: ExternalDatabase) -> None:
    """(Re)schedule the periodic job for one ExternalDatabase, or remove it
    if auto-refresh is off / not configured. Call this any time a
    database's config changes (create/update/delete), not just at startup."""
    try:
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler is not installed; external auto-refresh is disabled")
        return

    scheduler = get_scheduler()
    if scheduler is None:
        return
    job_id = _job_id(database.id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if not (database.is_enabled and database.auto_refresh_enabled and database.refresh_interval_minutes):
        return

    scheduler.add_job(
        _run_job,
        trigger=IntervalTrigger(minutes=database.refresh_interval_minutes),
        id=job_id,
        args=[database.id],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Scheduled external database %s every %s minute(s)", database.id, database.refresh_interval_minutes
    )


def unschedule_database(database_id: int) -> None:
    scheduler = get_scheduler()
    if scheduler is None:
        return
    job_id = _job_id(database_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def init_all_jobs() -> None:
    """Load every enabled auto-refresh database and schedule it. Call once
    on app startup (app/main.py lifespan)."""
    try:
        # Optional dependency: deployments that don't enable external auto-refresh
        # should still start cleanly without APScheduler installed.
        from apscheduler.triggers.interval import IntervalTrigger  # noqa: F401
    except ImportError:
        logger.warning("APScheduler is not installed; skipping external scheduler startup")
        return
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        databases = db.scalars(
            select(ExternalDatabase).where(
                ExternalDatabase.is_enabled.is_(True),
                ExternalDatabase.auto_refresh_enabled.is_(True),
            )
        ).all()
        for database in databases:
            schedule_database(database)
        logger.info("External data scheduler: %d auto-refresh job(s) loaded", len(databases))
    except Exception:
        logger.exception("Failed to load external data scheduler jobs")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 通讯中断自动巡检（AIMS 1.0 阶段 1，审计项 F-6）
#
# POST /communication/scan-stale 的逻辑以前从未被任何调度者调用——独立桌面
# 部署时通讯中断永远只有人工点按钮才会被发现。现在按固定间隔自动巡检，
# 与端点共用 app/services/data_health.scan_stale_tags 的同一实现。
# ---------------------------------------------------------------------------
COMM_STALE_SCAN_JOB_ID = "comm_stale_scan"


def schedule_comm_stale_scan(interval_minutes: int = 5, stale_minutes: int = 15) -> None:
    """Register (or replace) the periodic comm-stale scan job. Call once on
    app startup alongside init_all_jobs()."""
    scheduler = get_scheduler()
    if scheduler is None:
        return
    if scheduler.get_job(COMM_STALE_SCAN_JOB_ID):
        scheduler.remove_job(COMM_STALE_SCAN_JOB_ID)
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        _run_comm_stale_scan,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=COMM_STALE_SCAN_JOB_ID,
        args=[stale_minutes],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduled comm-stale scan every %d minute(s)", interval_minutes)


def _run_comm_stale_scan(stale_minutes: int) -> None:
    from app.services.data_health import scan_stale_tags

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        out = scan_stale_tags(db, stale_minutes)
        if out.comm_alarms_created:
            logger.warning("Comm-stale scan: %d new comm alarm(s)", out.comm_alarms_created)
    except Exception:
        logger.exception("Scheduled comm-stale scan failed")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 数据保留清理任务（AIMS 1.0 阶段 1，审计项 D-1）
#
# measurements / audit_logs 等表以前无限增长（data_retention_days 键没有任何
# 消费者）。现在每日凌晨 03:00 自动按 system_settings 里的保留天数清理。
# ---------------------------------------------------------------------------
RETENTION_JOB_ID = "retention_daily"


def schedule_retention(hour: int = 3) -> None:
    """Register the daily retention job (default 03:00 UTC). Call once on app
    startup. Also runs one sweep at startup so long-idle machines catch up."""
    scheduler = get_scheduler()
    if scheduler is None:
        return
    if scheduler.get_job(RETENTION_JOB_ID):
        scheduler.remove_job(RETENTION_JOB_ID)
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        _run_retention,
        trigger=CronTrigger(hour=hour, minute=30),
        id=RETENTION_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduled daily retention job at %02d:30 UTC", hour)


def _run_retention() -> None:
    from app.services.retention import run_retention

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        run_retention(db)
    except Exception:
        logger.exception("Scheduled retention run failed")
    finally:
        db.close()
