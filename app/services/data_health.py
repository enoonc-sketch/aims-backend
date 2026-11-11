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


# ---------------------------------------------------------------------------
# 通讯中断巡检（AIMS 1.0 阶段 1，审计项 F-6）
#
# 以前这段逻辑只存在于 POST /communication/scan-stale 端点里，全代码库没有任何
# 调度者调用它——独立桌面部署时通讯中断永远不会被发现。现在逻辑下沉到服务层：
# API 端点和 external_scheduler 里的定时任务调用同一个函数，行为完全一致。
# ---------------------------------------------------------------------------
def scan_stale_tags(db: Session, stale_minutes: int = 15):
    """找出绑定了网关但很久没有新数据的 TAG（通讯中断），开「通讯中断」报警，
    并把整个网关/设备置 offline。数据恢复（再次 ingest）时报警自动关闭。

    从没上报过任何数据的 TAG 不算中断（更可能是配置问题），只在
    never_reported_tags 计数。返回 StaleScanOut（与 API 响应模型一致）。
    """
    from app.models.device import Device, DeviceComponent, Gateway
    from app.services import data_chain
    from app.schemas.communication import StaleScanOut

    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=stale_minutes)

    tags = db.scalars(select(DeviceComponent).where(DeviceComponent.gateway_id.is_not(None))).all()
    device_ids = {t.device_id for t in tags}
    last: dict[tuple, datetime] = {}
    if device_ids:
        for dev_id, comp, ts in db.execute(
            select(Measurement.device_id, Measurement.component_name, func.max(Measurement.time))
            .where(Measurement.device_id.in_(device_ids))
            .group_by(Measurement.device_id, Measurement.component_name)
        ).all():
            last[(dev_id, comp)] = _aware(ts)

    gateways = {g.id: g for g in db.scalars(select(Gateway)).all()}
    devices = {d.id: d for d in db.scalars(select(Device).where(Device.id.in_(device_ids))).all()} if device_ids else {}

    result = StaleScanOut(stale_minutes=stale_minutes, checked_tags=len(tags))
    gw_state: dict[int, list[bool]] = {}
    dev_state: dict[int, list[bool]] = {}
    for tag in tags:
        lt = last.get((tag.device_id, tag.component_name))
        if lt is None:
            result.never_reported_tags += 1
            continue
        age = now - lt
        is_stale = age >= threshold
        gw_state.setdefault(tag.gateway_id, []).append(is_stale)
        dev_state.setdefault(tag.device_id, []).append(is_stale)
        if not is_stale:
            continue
        device, gw = devices[tag.device_id], gateways.get(tag.gateway_id)
        minutes = int(age.total_seconds() // 60)
        entry = {
            "device_code": device.device_code, "component_name": tag.component_name,
            "gateway_code": gw.gateway_code if gw else None, "tag_address": tag.tag_address,
            "last_data_at": lt.isoformat(), "silent_minutes": minutes,
        }
        if not data_chain.open_alarm_exists(db, device.id, component_name=tag.component_name, alarm_type="comm"):
            alarm, _ = data_chain.raise_alarm(
                db, device, tag.component_name, alarm_type="comm", severity="warning",
                message=(
                    f"{device.device_code} {tag.component_name} 数据中断 {minutes} 分钟"
                    f"（网关 {gw.gateway_code if gw else '-'}，地址 {tag.tag_address or '-'}）"
                ),
                triggered_at=now,
            )
            entry["alarm_id"] = alarm.id
            result.comm_alarms_created += 1
        result.stale_tags.append(entry)

    for gid, flags in gw_state.items():
        gw = gateways.get(gid)
        if gw is not None and flags and all(flags) and gw.status != "offline":
            gw.status = "offline"
            result.gateways_marked_offline.append(gw.gateway_code)
    for did, flags in dev_state.items():
        dev = devices.get(did)
        if dev is not None and flags and all(flags) and dev.status == "online":
            dev.status = "offline"
            result.devices_marked_offline.append(dev.device_code)

    db.commit()
    return result
