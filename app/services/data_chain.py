"""Auto-linking pipeline for the relationship chain that every import
endpoint should feed into, instead of writing rows into an isolated table:

    设备(Device) -> TAG(DeviceComponent) -> 通讯(Gateway)
        -> 实时数据(Measurement) -> 数据质量 -> 报警(Alarm) -> AI诊断(AIDiagnosis)
        -> 知识库(KB 摘录 / SOP) -> 工单(WorkOrder) -> 维修结果(MaintenanceRecord)
        -> AI反馈(AIDiagnosisFeedback)
    设备(Device) -> 资产(Asset)
    设备(Device) -> 实验室(LabSample / LabCorrelationResult)
    设备型号(DeviceModel) -> 知识库(MaintenanceSOP / KnowledgeDocument)

Every per-module `/import` endpoint (devices, gateways, analysis/measurements,
lab, assets) calls into this module so that importing data from any one of
those modules doesn't just insert rows into its own table — it also ensures
the device has the TAG it needs, that the TAG is linked to the communication
channel that feeds it, and that a value breaching a configured limit produces
an Alarm and, from that Alarm, a WorkOrder — the same chain a real value
arriving over the wire would go through, whether the value got there via
live polling or a spreadsheet import.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis
from app.models.alarm import Alarm, AlarmRule
from app.models.device import Device, DeviceComponent, Gateway, GatewayDevice
from app.models.maintenance import WorkOrder
from app.models.measurement import Measurement
from app.services import data_quality, diagnosis_flow
from app.services.alarms_hooks import maybe_create_work_order_for_alarm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- TAG -----
def get_or_create_tag(
    db: Session,
    device: Device,
    component_name: str,
    *,
    gateway: Optional[Gateway] = None,
    unit: Optional[str] = None,
    low_limit: Optional[float] = None,
    high_limit: Optional[float] = None,
    tag_address: Optional[str] = None,
) -> DeviceComponent:
    """设备 -> TAG: get the DeviceComponent (tag) for (device, component_name),
    creating it if this is the first time data/config for that component has
    ever shown up for this device. Any explicitly-given fields (gateway,
    limits, address) are applied even to an existing tag — later imports can
    fill in comms/limits that an earlier one didn't know yet — but never
    clear a value the tag already has."""
    tag = db.scalar(
        select(DeviceComponent).where(
            DeviceComponent.device_id == device.id,
            DeviceComponent.component_name == component_name,
        )
    )
    if tag is None:
        tag = DeviceComponent(
            device_id=device.id,
            component_name=component_name,
            unit=unit or "%",
            low_limit=low_limit,
            high_limit=high_limit,
            gateway_id=gateway.id if gateway else None,
            tag_address=tag_address,
        )
        db.add(tag)
        # 并发安全（审计项 D-6）：两个并发批次（实时推送+导入）可能同时走到这里，
        # 撞 (device_id, component_name) 唯一索引。用 SAVEPOINT 包住插入——失败只
        # 回滚这次插入（保留本事务里已完成的工作），然后采用并发方写入的那一行。
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            tag = db.scalar(
                select(DeviceComponent).where(
                    DeviceComponent.device_id == device.id,
                    DeviceComponent.component_name == component_name,
                )
            )
            if tag is None:
                raise
        else:
            if gateway is not None:
                link_gateway(db, device, gateway)
            return tag

    if gateway is not None and tag.gateway_id is None:
        tag.gateway_id = gateway.id
        link_gateway(db, device, gateway)
    if unit and tag.unit in (None, "", "%"):
        tag.unit = unit
    if low_limit is not None and tag.low_limit is None:
        tag.low_limit = low_limit
    if high_limit is not None and tag.high_limit is None:
        tag.high_limit = high_limit
    if tag_address and not tag.tag_address:
        tag.tag_address = tag_address
    return tag


# ------------------------------------------------------------ 通讯 --------
def link_gateway(db: Session, device: Device, gateway: Gateway) -> None:
    """TAG -> 通讯: make sure this device is associated with this gateway
    (the many-to-many join used by the Communication page to show which
    devices ride which gateway)."""
    existing = db.get(GatewayDevice, {"gateway_id": gateway.id, "device_id": device.id})
    if existing is None:
        db.add(GatewayDevice(gateway_id=gateway.id, device_id=device.id))


# ------------------------------------------------------- 报警 / 维护 -------
_OPEN_STATUSES = ("active", "acknowledged")


@dataclass
class ChainOutcome:
    """一次入库经过整条链之后, 产出了什么。"""

    quality: dict = field(default_factory=lambda: {"good": 0, "uncertain": 0, "bad": 0})
    alarms: list[Alarm] = field(default_factory=list)          # 工艺阈值报警
    dq_alarms: list[Alarm] = field(default_factory=list)       # 数据质量报警
    comm_alarms_resolved: int = 0                              # 数据恢复后自动关闭的通讯中断报警
    diagnoses: list[AIDiagnosis] = field(default_factory=list)
    work_orders: list[WorkOrder] = field(default_factory=list)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def open_alarm_exists(
    db: Session, device_id: int, *, rule_id: Optional[int] = None, component_name: Optional[str] = None,
    alarm_type: str = "process",
) -> bool:
    """同一条规则(或同一点位的同类报警)上是否已有"未结案"的报警。

    「未结案」= active **或 acknowledged**。之前只查 active: 报警被确认(acknowledged)之后
    下一个超限读数会再开一条报警、再开一张工单, 同一个故障两张单。"""
    stmt = select(Alarm.id).where(
        Alarm.device_id == device_id, Alarm.alarm_type == alarm_type, Alarm.status.in_(_OPEN_STATUSES)
    )
    if rule_id is not None:
        stmt = stmt.where(Alarm.rule_id == rule_id)
    else:
        stmt = stmt.where(Alarm.component_name == component_name)
    return db.scalar(stmt.limit(1)) is not None


def on_alarm_created(db: Session, alarm: Alarm) -> tuple[Optional[AIDiagnosis], Optional[WorkOrder]]:
    """报警 -> AI诊断 -> 知识库 -> 工单。所有创建报警的路径(阈值 / 数据质量 / 通讯 / 手动 POST)都走这里,
    所以每条报警都带着诊断和知识库依据进工单, 而不是各路径各建各的。

    诊断失败不能吞掉报警和工单(报警是安全相关的, AI 是辅助): 失败只记日志, 工单照建(不带诊断)。"""
    diagnosis: Optional[AIDiagnosis] = None
    try:
        diagnosis = diagnosis_flow.diagnose_alarm(db, alarm, source="alarm_auto")
    except Exception:  # noqa: BLE001
        logger.exception("auto diagnosis failed for alarm %s; continuing without it", alarm.id)
    wo = maybe_create_work_order_for_alarm(db, alarm, diagnosis=diagnosis)
    if wo is not None:
        db.flush()
    return diagnosis, wo


def _new_alarm(
    db: Session, out: ChainOutcome, *, device: Device, component_name: str, alarm_type: str, severity: str,
    message: str, value: Optional[float], rule_id: Optional[int], triggered_at: Optional[datetime],
) -> Alarm:
    alarm = Alarm(
        device_id=device.id, rule_id=rule_id, component_name=component_name, alarm_type=alarm_type,
        severity=severity, message=message, value=value, status="active",
    )
    if triggered_at is not None:
        # 报警发生时间 = 触发它的那条读数的时间(而不是"入库的这一刻"): 诊断取证据窗口要对齐它
        alarm.triggered_at = triggered_at
    db.add(alarm)
    db.flush()
    diagnosis, wo = on_alarm_created(db, alarm)
    if diagnosis is not None:
        out.diagnoses.append(diagnosis)
    if wo is not None:
        out.work_orders.append(wo)
    return alarm


def _breach_direction(rule: AlarmRule, value: float) -> Optional[str]:
    if rule.low_limit is not None and value < float(rule.low_limit):
        return "低于下限"
    if rule.high_limit is not None and value > float(rule.high_limit):
        return "高于上限"
    return None


def _evaluate_component(
    db: Session, device: Device, component_name: str, points: list, out: ChainOutcome,
) -> None:
    """points: [(time, value, Assessment)] 按时间升序。规则只查一次(而不是每行一次)。

    - bad 质量的读数不参与阈值判断(坏数据不该产生工艺报警);
    - uncertain 的读数照常判断, 报警信息里注明存疑原因;
    - 每条规则只在还没有未结案报警时才开新报警, 报警值是**第一条**越限读数。"""
    rules = db.scalars(
        select(AlarmRule).where(
            AlarmRule.component_name == component_name,
            AlarmRule.is_enabled.is_(True),
            (AlarmRule.device_id == device.id) | (AlarmRule.device_id.is_(None)),
        )
    ).all()
    for rule in rules:
        if open_alarm_exists(db, device.id, rule_id=rule.id):
            continue
        for ts, value, assessment in points:
            if assessment.quality == data_quality.BAD:
                continue
            direction = _breach_direction(rule, value)
            if direction is None:
                continue
            note = (
                f"（数据质量存疑：{data_quality.describe(assessment.reasons)}）"
                if assessment.quality == data_quality.UNCERTAIN else ""
            )
            out.alarms.append(
                _new_alarm(
                    db, out, device=device, component_name=component_name, alarm_type="process",
                    severity=rule.severity,
                    message=f"{device.device_code} {component_name} 读数 {value:g} {direction}（规则 #{rule.id}）{note}",
                    value=value, rule_id=rule.id, triggered_at=ts,
                )
            )
            break


def evaluate_alarm_rules(db: Session, device: Device, component_name: str, value: float) -> list[Alarm]:
    """实时数据 -> 报警 -> AI诊断 -> 工单 (单个读数的兼容入口, 假定质量 good)。
    新代码请用 process_readings, 它会先做数据质量评估。"""
    out = ChainOutcome()
    _evaluate_component(db, device, component_name, [(None, value, data_quality.Assessment())], out)
    return out.alarms


def _bulk_upsert_measurements(db: Session, rows: list[Measurement], chunk: int = 5_000) -> None:
    """写入读数；主键冲突时更新，而不是抛 IntegrityError。

    (time, device_id, component_name) 是 measurements 的主键。同一时刻同一测点被
    重复上报/重复导入，在工业现场是常态：网关断线重连会重发缓存数据、现场补录会
    重导同一份 CSV、外部云同步会重叠时间窗。原来的 `bulk_save_objects` 遇到冲突
    直接抛 UniqueViolation，用户看到 HTTP 500 + 一整页堆栈，而数据其实只是重复。

    这里改成幂等写入：已存在则用本次的值/质量覆盖（这正符合"重导是为了修正"的
    现场语义），行数统计仍按本次提交的行数返回。

    分块仍保留（默认 5000 行/批，避免超参数上限与峰值内存翻倍），单事务语义不变。
    """
    if not rows:
        return
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    dialect = getattr(getattr(bind, "dialect", None), "name", "")

    def as_dict(r: Measurement) -> dict:
        return {
            "time": r.time,
            "device_id": r.device_id,
            "component_name": r.component_name,
            "value": r.value,
            "quality": r.quality,
            "import_id": r.import_id,
        }

    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        if dialect in ("postgresql", "sqlite"):
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            else:
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            # 用 executemany（把参数字典列表交给 execute）而不是 insert().values([...])：
            # 后者会编译成一条几万个参数的巨型 SQL，PG 解析/绑定开销随行数非线性上升，
            # 实测 2 万行从 ~1.9s 退化到 ~10s。executemany 走 psycopg2 的批量执行，
            # 单行语义不变（冲突仍按 ON CONFLICT DO UPDATE 处理）。
            stmt = dialect_insert(Measurement)
            stmt = stmt.on_conflict_do_update(
                index_elements=["time", "device_id", "component_name"],
                set_={
                    "value": stmt.excluded.value,
                    "quality": stmt.excluded.quality,
                    "import_id": stmt.excluded.import_id,
                },
            )
            db.execute(stmt, [as_dict(r) for r in part])
        else:
            # 其它方言（理论上不会走到）保持原行为，至少不静默丢数据
            db.bulk_save_objects(part)


def process_readings(
    db: Session, device: Device, rows: list[Measurement], *, realtime: bool,
) -> ChainOutcome:
    """把一批尚未写库的读数送过整条链:

        数据质量评估(就地改 row.quality) -> 写库 -> TAG(确保存在)
        -> 报警(阈值 / 连续坏数据) -> AI诊断 -> 知识库 -> 工单
        -> (仅 realtime) 数据恢复则自动关闭该点位的「通讯中断」报警

    所有入库路径(单条 / 批量 / 文件导入 / 外部云数据 / 网关推送)都必须走这里,
    而不是各自 add 行再各自判断阈值。`realtime=False` 表示历史回填: 不做"时间戳过旧"判断,
    也不因此关闭通讯中断报警。调用方负责 commit。"""
    out = ChainOutcome()
    if not rows:
        return out

    report = data_quality.assess_rows(db, device, rows, realtime=realtime)
    out.quality = report.summary()
    # 审计项 D-2：分块写入降低峰值内存（10 万行一次 bulk_save_objects 会临时
    # 翻倍占用），仍保持单事务语义——外部同步等调用方依赖"中途失败整体回滚"，
    # 这里不能中途提交；孤儿 processing 批次由启动时的
    # retention.recover_orphan_imports 回收。
    _bulk_upsert_measurements(db, rows)

    now = datetime.now(timezone.utc)
    by_comp: dict[str, list[Measurement]] = {}
    for r in rows:
        by_comp.setdefault(r.component_name, []).append(r)

    for comp, comp_rows in by_comp.items():
        comp_rows.sort(key=lambda r: _aware(r.time) or now)
        get_or_create_tag(db, device, comp)
        points = [(_aware(r.time), r.value, report.by_row[id(r)]) for r in comp_rows]
        _evaluate_component(db, device, comp, points, out)

        # 数据质量报警: 本批末尾连续 bad; 若本批全 bad, 再加上本批之前已连续的 bad
        run = report.trailing_bad.get(comp, 0)
        if report.all_bad.get(comp):
            run += data_quality.prior_bad_run(db, device.id, comp, _aware(comp_rows[0].time))
        if run >= data_quality.DQ_ALARM_CONSECUTIVE_BAD and not open_alarm_exists(
            db, device.id, component_name=comp, alarm_type="data_quality"
        ):
            why = data_quality.describe(report.reasons.get(comp, {}).keys())
            out.dq_alarms.append(
                _new_alarm(
                    db, out, device=device, component_name=comp, alarm_type="data_quality", severity="warning",
                    message=f"{device.device_code} {comp} 连续 {run} 条读数质量为 bad（{why or '设备上报'}）",
                    value=None, rule_id=None, triggered_at=_aware(comp_rows[-1].time),
                )
            )

        # 通讯中断报警在数据恢复后自动关闭 (工艺报警不会: 它们需要人确认); 工单仍留给人处理
        if realtime and report.trailing_bad.get(comp, 0) == 0:
            for a in db.scalars(
                select(Alarm).where(
                    Alarm.device_id == device.id, Alarm.component_name == comp,
                    Alarm.alarm_type == "comm", Alarm.status.in_(_OPEN_STATUSES),
                )
            ).all():
                a.status = "resolved"
                a.resolved_at = now
                out.comm_alarms_resolved += 1
    return out


def raise_alarm(
    db: Session, device: Device, component_name: str, *, alarm_type: str, severity: str, message: str,
    value: Optional[float] = None, rule_id: Optional[int] = None, triggered_at: Optional[datetime] = None,
) -> tuple[Alarm, ChainOutcome]:
    """非读数触发的报警(如通讯中断扫描)的入口: 与阈值报警走同一条 报警->AI诊断->知识库->工单 链。"""
    out = ChainOutcome()
    alarm = _new_alarm(
        db, out, device=device, component_name=component_name, alarm_type=alarm_type, severity=severity,
        message=message, value=value, rule_id=rule_id, triggered_at=triggered_at,
    )
    return alarm, out
