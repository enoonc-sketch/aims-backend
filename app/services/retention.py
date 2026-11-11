"""数据保留 / 清理服务（AIMS 1.0 阶段 1，审计项 D-1）。

审计结论：`data_retention_days` 这个键在种子数据里写了 365 天，但全代码库
没有任何消费者——measurements / acquisition_events / audit_logs /
dashboard_snapshots / external_sync_logs / login_failures 全部无限增长，
时序表最终会拖垮整机磁盘。

本模块提供：
- `run_retention(db, now=None)` —— 按保留天数清理各表，返回每张表删除的行数。
  保留天数从 system_settings 读取（默认 365），key 见 RETENTION_KEYS。
- `recover_orphan_imports(db)` —— 把崩溃/中断后永久停留在 processing 的
  导入批次标记为 failed（审计项 D-2 的一半；另一半是 process_readings
  里的分批提交）。

由 external_scheduler 的每日任务（retention_daily）调用；也可通过
`python -m app.deploy.cli maintenance` 手工触发。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.analysis import DashboardSnapshot
from app.models.external_source import ExternalSyncLog
from app.models.industrial import AcquisitionEvent
from app.models.measurement import Measurement, MeasurementImport
from app.models.system import AuditLog, LoginFailure, SystemSetting

logger = logging.getLogger("aims.retention")

# key -> (说明, 默认天数)。新增可清理表时在这里登记即可。
RETENTION_KEYS: dict[str, tuple[str, int]] = {
    "data_retention_days": ("测量读数保留天数", 365),
    "audit_log_retention_days": ("审计日志保留天数", 730),
    "snapshot_retention_days": ("看板快照保留天数", 180),
    "sync_log_retention_days": ("外部同步日志保留天数", 180),
    "acquisition_event_retention_days": ("采集事件保留天数", 365),
    "login_failure_retention_days": ("登录失败记录保留天数", 30),
}

# 登录失败记录量小且自愈，单独用较短默认值


def get_retention_days(db: Session, key: str) -> int:
    desc, default = RETENTION_KEYS[key]
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is None or not row.value:
        return default
    try:
        days = int(row.value)
    except (TypeError, ValueError):
        return default
    return days if days > 0 else default


def _chunked_delete(db: Session, model, where_clause, *, chunk_rows: int = 5000) -> int:
    """分块执行 DELETE，避免一次性锁住大表/产生超大事务。

    每轮先 SELECT 一批主键，再按主键 IN 删除并提交（SQLite 与 PostgreSQL
    通用写法）。返回删除总行数。
    """
    pk = list(model.__table__.primary_key.columns)[0]
    total = 0
    for _round in range(100000):  # 硬上限防意外死循环
        ids = db.execute(select(pk).where(where_clause).limit(chunk_rows)).scalars().all()
        if not ids:
            break
        db.execute(delete(model).where(pk.in_(ids)))
        db.commit()
        total += len(ids)
    return total


def run_retention(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """按 system_settings 里的保留天数清理各表。返回 {key: deleted_rows}。"""
    now = now or datetime.now(timezone.utc)
    result: dict[str, int] = {}

    def cutoff(key: str) -> datetime:
        return now - timedelta(days=get_retention_days(db, key))

    # 测量读数（最大的表，最先清）
    c = cutoff("data_retention_days")
    result["data_retention_days"] = _chunked_delete(
        db, Measurement, Measurement.time < c
    )

    # 采集事件（工业域高频表）
    c = cutoff("acquisition_event_retention_days")
    result["acquisition_event_retention_days"] = _chunked_delete(
        db, AcquisitionEvent, AcquisitionEvent.event_time < c
    )

    # 审计日志
    c = cutoff("audit_log_retention_days")
    result["audit_log_retention_days"] = _chunked_delete(
        db, AuditLog, AuditLog.created_at < c
    )

    # 看板快照
    c = cutoff("snapshot_retention_days")
    result["snapshot_retention_days"] = _chunked_delete(
        db, DashboardSnapshot, DashboardSnapshot.snapshot_at < c
    )

    # 外部同步日志
    c = cutoff("sync_log_retention_days")
    result["sync_log_retention_days"] = _chunked_delete(
        db, ExternalSyncLog, ExternalSyncLog.started_at < c
    )

    # 登录失败记录（审计报告 L 项：模型注释承诺 purge 但从未实现）
    c = cutoff("login_failure_retention_days")
    result["login_failure_retention_days"] = _chunked_delete(
        db, LoginFailure, LoginFailure.created_at < c
    )

    logger.info("Retention run: %s", {k: v for k, v in result.items() if v})
    return result


def recover_orphan_imports(db: Session, *, stale_minutes: int = 60) -> int:
    """把停留在 processing 超过 stale_minutes 的导入批次标记为 failed。

    导入端点先以 status=processing 提交批次，成功后才改成 done；进程在
    中间崩溃/被杀时批次会永远停留，数据页将显示一个虚假的"导入中"。
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=stale_minutes)
    orphans = db.scalars(
        select(MeasurementImport).where(
            MeasurementImport.status == "processing",
            MeasurementImport.created_at < threshold,
        )
    ).all()
    for row in orphans:
        row.status = "failed"
    if orphans:
        db.commit()
        logger.warning("Marked %d orphan import batch(es) as failed", len(orphans))
    return len(orphans)
