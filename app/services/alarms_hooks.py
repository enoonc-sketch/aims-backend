"""Business hooks around alarms — auto-create work orders for warning/critical
severity. Shared by the manual `POST /alarms` endpoint and by
app/services/data_chain.py's automatic threshold-breach pipeline, so both
paths produce exactly one work order per alarm with the same priority rule.

知识库 -> 工单: 工单不再只有一行报警标题。若该报警已有 AI 诊断, 工单带上
diagnosis_id / sop_id, 描述里写明诊断、建议、知识库摘录与建议 SOP, 维修人员打开工单就能开工。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis
from app.models.alarm import Alarm
from app.models.maintenance import WorkOrder

_SEVERITY_TO_PRIORITY = {"critical": "urgent", "warning": "medium", "info": "low"}
# 通讯中断 / 数据质量问题先是"去现场查", 不是"直接修": 工单类型用 inspection
_ORDER_TYPE_BY_ALARM_TYPE = {"comm": "inspection", "data_quality": "inspection"}


def attach_diagnosis_to_work_order(db: Session, wo: WorkOrder, alarm: Optional[Alarm], diagnosis: AIDiagnosis) -> None:
    from app.services.diagnosis_flow import work_order_brief

    wo.diagnosis_id = diagnosis.id
    sop = (diagnosis.evidence or {}).get("sop")
    wo.sop_id = sop["id"] if sop else wo.sop_id
    wo.description = work_order_brief(alarm, diagnosis)


def maybe_create_work_order_for_alarm(
    db: Session, alarm: Alarm, diagnosis: Optional[AIDiagnosis] = None
) -> WorkOrder | None:
    """If alarm is warning/critical severity and no open work order already
    linked to it, create one — priority follows severity. 'info' alarms don't
    warrant a work order on their own. When `diagnosis` is given, the work
    order carries it (and the knowledge-base excerpts / SOP it cites)."""
    severity = (alarm.severity or "").lower() if alarm else ""
    if not alarm or severity not in ("warning", "critical"):
        return None

    # avoid duplicates: existing open WO for same alarm
    existing = db.scalar(
        select(WorkOrder).where(
            WorkOrder.alarm_id == alarm.id,
            WorkOrder.status.in_(["open", "in_progress"]),
        )
    )
    if existing:
        if diagnosis is not None and existing.diagnosis_id is None:
            attach_diagnosis_to_work_order(db, existing, alarm, diagnosis)
        return existing

    title = alarm.message or f"Alarm on component {alarm.component_name or '-'}"
    if len(title) > 200:
        title = title[:197] + "..."

    wo = WorkOrder(
        device_id=alarm.device_id,
        alarm_id=alarm.id,
        title=title,
        order_type=_ORDER_TYPE_BY_ALARM_TYPE.get(alarm.alarm_type or "process", "repair"),
        priority=_SEVERITY_TO_PRIORITY.get(severity, "medium"),
        status="open",
    )
    db.add(wo)
    if diagnosis is not None:
        attach_diagnosis_to_work_order(db, wo, alarm, diagnosis)
    # caller is responsible for commit
    return wo


def refresh_open_work_order(db: Session, alarm: Alarm, diagnosis: AIDiagnosis) -> Optional[WorkOrder]:
    """重跑诊断后, 用新诊断刷新这条报警上仍未完成的工单(已完成的工单是历史记录, 不改)。"""
    wo = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.alarm_id == alarm.id, WorkOrder.status.in_(["open", "in_progress"]))
        .order_by(WorkOrder.id.desc())
    )
    if wo is not None:
        attach_diagnosis_to_work_order(db, wo, alarm, diagnosis)
    return wo
