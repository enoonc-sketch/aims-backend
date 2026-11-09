"""数据连续性 / 可维护性指标，以及和实验室化验样品的挂钩。

- coverage_summary: 某设备某组分在一个时间窗内的数据覆盖率、断点数、最长断点、最后一条数据时间
- database_health: 某个外部数据库的抓取可维护性（成功率、连续失败次数、最近成功时间）
- lab_sample_coverage: 每条化验样品在采样时刻前后窗口内有没有在线数据；没有的话指出是哪个断点造成的
  （这就是"实验室对比/AI 认证配不上对"的最常见原因——网站那段时间没数据）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.external_source import DataContinuityGap, ExternalDatabase, ExternalSyncLog
from app.models.maintenance import LabSample
from app.models.measurement import Measurement


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def coverage_summary(
    db: Session,
    device_id: int,
    component_name: str,
    *,
    expected_interval_minutes: int = 60,
    hours: int = 168,
) -> dict:
    expected = max(int(expected_interval_minutes), 1)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    times = [
        _aware(t)
        for t in db.scalars(
            select(Measurement.time)
            .where(
                Measurement.device_id == device_id,
                Measurement.component_name == component_name,
                Measurement.time >= since,
            )
            .order_by(Measurement.time.asc())
        ).all()
    ]

    expected_points = max(int(hours * 60 / expected), 1)
    gap_threshold = expected * 2.0
    gaps = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(times, times[1:])
        if (b - a).total_seconds() / 60.0 >= gap_threshold
    ]
    last = times[-1] if times else None
    # 最后一条数据到现在也算一段"进行中的断点"
    trailing = (now - last).total_seconds() / 60.0 if last else hours * 60.0
    open_gap = trailing >= gap_threshold

    return {
        "device_id": device_id,
        "component_name": component_name,
        "window_hours": hours,
        "expected_interval_minutes": expected,
        "expected_points": expected_points,
        "actual_points": len(times),
        "coverage_pct": round(min(len(times) / expected_points, 1.0) * 100, 1),
        "gap_count": len(gaps) + (1 if open_gap else 0),
        "longest_gap_minutes": round(max(gaps + ([trailing] if open_gap else []), default=0.0), 1),
        "last_data_at": last,
        "currently_stale": open_gap,
    }


def database_health(db: Session, database: ExternalDatabase, *, recent: int = 30) -> dict:
    logs = db.scalars(
        select(ExternalSyncLog)
        .where(ExternalSyncLog.database_id == database.id, ExternalSyncLog.status != "running")
        .order_by(ExternalSyncLog.id.desc())
        .limit(recent)
    ).all()
    ok = [l for l in logs if l.status == "success"]
    consecutive_failures = 0
    for l in logs:
        if l.status == "failed":
            consecutive_failures += 1
        else:
            break
    open_gaps = db.scalar(
        select(func.count())
        .select_from(DataContinuityGap)
        .where(DataContinuityGap.database_id == database.id, DataContinuityGap.resolved.is_(False))
    )
    return {
        "database_id": database.id,
        "name": database.name,
        "runs_considered": len(logs),
        "success_rate_pct": round(len(ok) / len(logs) * 100, 1) if logs else None,
        "consecutive_failures": consecutive_failures,
        "last_success_at": ok[0].finished_at if ok else None,
        "last_error": database.last_error,
        "unresolved_gaps": int(open_gaps or 0),
    }


def lab_sample_coverage(
    db: Session,
    device_id: int,
    component_name: str,
    *,
    window_minutes: float = 30.0,
) -> dict:
    """For each lab sample: is there at least one online reading within
    ±window_minutes of sampled_at? If not, name the continuity gap (if one
    was recorded) that covers that moment."""
    samples = db.scalars(
        select(LabSample)
        .where(LabSample.device_id == device_id, LabSample.component_name == component_name)
        .order_by(LabSample.sampled_at.asc())
    ).all()
    gaps = db.scalars(
        select(DataContinuityGap).where(
            DataContinuityGap.device_id == device_id,
            DataContinuityGap.component_name == component_name,
        )
    ).all()
    w = timedelta(minutes=window_minutes)

    items = []
    for s in samples:
        t = _aware(s.sampled_at)
        if t is None:
            continue
        n = db.scalar(
            select(func.count())
            .select_from(Measurement)
            .where(
                Measurement.device_id == device_id,
                Measurement.component_name == component_name,
                Measurement.time >= t - w,
                Measurement.time <= t + w,
            )
        )
        gap = next(
            (g for g in gaps if _aware(g.gap_start) <= t <= _aware(g.gap_end)),
            None,
        )
        items.append(
            {
                "sample_id": s.id,
                "sample_no": s.sample_no,
                "sampled_at": t,
                "online_points_in_window": int(n or 0),
                "covered": bool(n),
                "explained_by_gap_id": gap.id if (gap and not n) else None,
            }
        )
    covered = sum(1 for i in items if i["covered"])
    return {
        "device_id": device_id,
        "component_name": component_name,
        "window_minutes": window_minutes,
        "sample_count": len(items),
        "covered_count": covered,
        "covered_pct": round(covered / len(items) * 100, 1) if items else None,
        "items": items,
    }
