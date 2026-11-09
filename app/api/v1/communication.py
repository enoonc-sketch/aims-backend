"""Communication test API — used by the Communication Management page to
verify a gateway/device is actually reachable (TCP handshake, or a real
Modbus TCP probe for Modbus devices), on demand.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, get_db
from app.models.device import Device, DeviceComponent, Gateway
from app.models.measurement import Measurement
from app.models.system import User
from app.schemas.communication import (
    CommTestRequest,
    CommTestResult,
    GatewayIngestIn,
    GatewayIngestOut,
    StaleScanOut,
)
from app.services import comm_test, data_chain

router = APIRouter(prefix="/communication", tags=["Communication"])


@router.post("/test", response_model=CommTestResult)
async def test_connection(
    payload: CommTestRequest, user: User = Depends(get_current_user)
) -> CommTestResult:
    """Ad-hoc communication test against any host/port — used for gateways
    (and anything else) that aren't necessarily tied to a devices/gateways
    DB row. Runs the blocking socket probe off the event loop.

    Requires auth: this endpoint will happily open a TCP/Modbus connection to
    *any* host/port the caller names, so left open it's a ready-made network
    probe/SSRF oracle for anyone who can reach the API, not just a gateway
    diagnostics tool for logged-in operators.
    """
    outcome = await run_in_threadpool(
        comm_test.run_test, payload.host, payload.port, payload.protocol, payload.timeout
    )
    return CommTestResult(
        success=outcome.success,
        latency_ms=outcome.latency_ms,
        message=outcome.message,
        detail=outcome.detail,
    )


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.post("/ingest", response_model=GatewayIngestOut)
def gateway_ingest(
    payload: GatewayIngestIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> GatewayIngestOut:
    """通讯 -> 实时数据: 网关/采集程序把读数推进来的**唯一真正的采集入口**。

    以前"通讯"只能被 ping(见 /test), 数据只能靠手工 POST 或文件导入进库, TAG 上配的
    gateway_id / tag_address 在整条链里从没被用来解析过任何一条数据。这里按
    (网关, tag_address) 找到 TAG -> 设备+组分, 然后走 data_chain.process_readings
    (数据质量 -> 报警 -> AI诊断 -> 知识库 -> 工单)。

    - 网关下没有 TAG 绑定的地址不会静默丢弃: 逐个列在 unmapped_addresses 里返回;
    - 重复的 (点位, 时间) 跳过(采集端重传是常态), 数量在 duplicates_skipped;
    - 收到数据即证明链路通: 网关置 online 并刷新心跳, 离线的设备置 online,
      该点位上的「通讯中断」报警自动关闭。
    """
    from app.models.measurement import Measurement as M

    gw = db.scalar(select(Gateway).where(Gateway.gateway_code == payload.gateway_code))
    if gw is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    tags = db.scalars(select(DeviceComponent).where(DeviceComponent.gateway_id == gw.id)).all()
    by_addr = {(t.tag_address or "").strip(): t for t in tags if (t.tag_address or "").strip()}

    now = datetime.now(timezone.utc)
    unmapped: list[str] = []
    keyed: dict[tuple, M] = {}  # (device_id, component, time) -> row; 同键后到的覆盖先到的
    for pt in payload.points:
        tag = by_addr.get(pt.tag_address.strip())
        if tag is None:
            if pt.tag_address not in unmapped:
                unmapped.append(pt.tag_address)
            continue
        ts = _aware(pt.time) or now
        keyed[(tag.device_id, tag.component_name, ts)] = M(
            time=ts, device_id=tag.device_id, component_name=tag.component_name, value=pt.value, quality=pt.quality,
        )
    accepted_total = len(keyed)

    # 跳过库里已有的 (点位, 时间)
    dup = 0
    by_series: dict[tuple, list[datetime]] = {}
    for (dev_id, comp, ts) in keyed:
        by_series.setdefault((dev_id, comp), []).append(ts)
    for (dev_id, comp), times in by_series.items():
        existing = set(
            _aware(t) for t in db.scalars(
                select(M.time).where(M.device_id == dev_id, M.component_name == comp, M.time.in_(times))
            ).all()
        )
        for ts in existing:
            if keyed.pop((dev_id, comp, ts), None) is not None:
                dup += 1

    rows_by_device: dict[int, list[M]] = {}
    for (dev_id, _c, _t), row in keyed.items():
        rows_by_device.setdefault(dev_id, []).append(row)

    result = GatewayIngestOut(
        gateway_code=gw.gateway_code, accepted=accepted_total - dup, duplicates_skipped=dup,
        unmapped_addresses=unmapped, quality={"good": 0, "uncertain": 0, "bad": 0},
    )
    for dev_id, rows in rows_by_device.items():
        device = db.get(Device, dev_id)
        out = data_chain.process_readings(db, device, rows, realtime=True)
        for k, v in out.quality.items():
            result.quality[k] = result.quality.get(k, 0) + v
        result.alarms_created += len(out.alarms)
        result.data_quality_alarms_created += len(out.dq_alarms)
        result.comm_alarms_resolved += out.comm_alarms_resolved
        result.work_orders_created += len(out.work_orders)
        if device.status == "offline":
            device.status = "online"

    if rows_by_device:  # 真的收到了有效数据才算链路通
        gw.status = "online"
        gw.last_heartbeat = now
    db.commit()
    return result


@router.post("/scan-stale", response_model=StaleScanOut)
def scan_stale_tags(
    stale_minutes: int = Query(15, ge=1, le=7 * 24 * 60, description="超过多少分钟没新数据视为通讯中断"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StaleScanOut:
    """通讯 -> 数据质量: 找出绑定了网关、但已经很久没有新数据的 TAG(采集断了)。

    对每个中断的点位开一条「通讯中断」报警(同一点位未结案时不重复开), 报警同样走
    AI诊断 -> 知识库 -> 工单; 若一个网关(或设备)下所有曾有数据的 TAG 都中断了, 网关/设备置 offline。
    数据恢复(网关再次 /ingest)时该报警自动关闭。

    从没上报过任何数据的 TAG 不算"中断"(更可能是配置问题), 只计数在 never_reported_tags。
    这个接口是无状态的, 由外部定时调用(cron / 计划任务 / 调度器)。
    """
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
