"""Orchestrates one end-to-end sync of a single ExternalDatabase: log into
the vendor site -> download this database's file -> import it into
measurements -> run the continuity check -> record an ExternalSyncLog.

Used by both the manual `POST /external/databases/{id}/refresh` endpoint and
the scheduler (app/services/external_scheduler.py), so both paths produce
the exact same log/status trail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.external_source import ExternalDatabase, ExternalSource, ExternalSyncLog
from app.services import data_continuity, external_import
from app.services.external_scraper import ScraperError, download_from_database, external_session

logger = logging.getLogger("aims.external_sync")


def _fail(db: Session, source: ExternalSource | None, database: ExternalDatabase, log: ExternalSyncLog, message: str) -> None:
    # The half-finished import was done inside a SAVEPOINT that the caller has
    # already rolled back; only the failure bookkeeping below is committed.
    log.status = "failed"
    log.error_message = message
    log.finished_at = datetime.now(timezone.utc)
    database.last_status = "failed"
    database.last_error = message
    if source is not None:
        source.status = "login_failed" if "登录" in message else "error"
        source.last_error = message
    db.commit()


def run_sync_for_database(db: Session, database: ExternalDatabase, *, triggered_by: str = "manual") -> ExternalSyncLog:
    """Runs synchronously (a real headless-browser session + file parse), so
    callers on the request path should expect this to take anywhere from a
    few seconds to ~1 minute depending on the target site."""
    source = db.get(ExternalSource, database.source_id)

    log = ExternalSyncLog(database_id=database.id, triggered_by=triggered_by, status="running")
    db.add(log)
    database.last_status = "running"
    db.commit()
    db.refresh(log)

    # Everything that writes measurements/gaps/alarms happens inside one
    # SAVEPOINT: a failure discards just this sync's partial import without
    # rolling back anything else that shares the session's transaction.
    savepoint = db.begin_nested()
    try:
        if source is None:
            raise RuntimeError("对应的数据源站点不存在或已被删除")
        if not source.is_enabled:
            raise RuntimeError(f"数据源「{source.name}」已被禁用")
        if not database.is_enabled:
            raise RuntimeError(f"数据库「{database.name}」已被禁用")

        download_dir = settings.external_download_dir_path / str(database.id)
        with external_session(source) as page:
            source.status = "ok"
            source.last_login_at = datetime.now(timezone.utc)
            source.last_error = None
            file_path = download_from_database(page, database, download_dir)

        log.file_name = file_path.name

        stats = external_import.import_database_file(db, database, file_path)
        log.rows_downloaded = stats.rows_downloaded
        log.rows_imported = stats.rows_imported

        gaps_found = data_continuity.check_continuity_for_database(db, database, stats.device_ids)
        log.gaps_found = gaps_found

        if stats.warnings:
            log.error_message = "; ".join(stats.warnings)

        database.last_synced_at = datetime.now(timezone.utc)
        database.last_status = "success"
        database.last_error = None
        database.last_rows_imported = stats.rows_imported
        log.status = "success"
        log.finished_at = datetime.now(timezone.utc)
        savepoint.commit()
        db.commit()
        db.refresh(log)
        logger.info(
            "External sync ok: database_id=%s downloaded=%s imported=%s gaps=%s",
            database.id, stats.rows_downloaded, stats.rows_imported, gaps_found,
        )
        return log
    except ScraperError as e:
        if savepoint.is_active:
            savepoint.rollback()
        _fail(db, source, database, log, str(e))
        return log
    except Exception as e:  # noqa: BLE001 — any failure must still land in the log/UI, not a bare 500
        logger.exception("External sync failed for database_id=%s", database.id)
        if savepoint.is_active:
            savepoint.rollback()
        _fail(db, source, database, log, str(e))
        return log
