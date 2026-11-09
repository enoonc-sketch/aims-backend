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
