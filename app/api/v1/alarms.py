"""Alarms API — list, acknowledge, resolve alarms; manage alarm rules."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.alarm import Alarm, AlarmRule
from app.models.system import User
from app.schemas.ai import DiagnosisOut
from app.schemas.alarm import AlarmAck, AlarmCreate, AlarmOut, AlarmRuleCreate, AlarmRuleOut

router = APIRouter(prefix="/alarms", tags=["Alarms"])


@router.get("", response_model=list[AlarmOut])
def list_alarms(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    status: Optional[str] = Query(None, description="active/acknowledged/resolved"),
    severity: Optional[str] = Query(None, description="critical/warning/normal"),
    skip: int = SkipQuery,
    limit: int = LimitQuery(100),
):
    stmt = select(Alarm)
    if device_id:
        stmt = stmt.where(Alarm.device_id == device_id)
    if status:
        stmt = stmt.where(Alarm.status == status)
    if severity:
        stmt = stmt.where(Alarm.severity == severity)
    stmt = stmt.order_by(Alarm.triggered_at.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("", response_model=AlarmOut, status_code=201)
def create_alarm(payload: AlarmCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    alarm = Alarm(**payload.model_dump())
    db.add(alarm)
    db.flush()  # get alarm.id before the diagnosis / work-order hook
    # 手动建的报警和阈值触发的报警走同一条链: AI诊断 -> 知识库 -> 工单
    from app.services import data_chain
    data_chain.on_alarm_created(db, alarm)
    db.commit()
    db.refresh(alarm)
    return alarm


@router.post("/{alarm_id}/acknowledge", response_model=AlarmOut)
def acknowledge_alarm(alarm_id: int, payload: AlarmAck, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    alarm = db.get(Alarm, alarm_id)
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")
    alarm.status = "acknowledged"
    alarm.acknowledged_by = payload.user_id
    alarm.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alarm)
    return alarm


@router.post("/{alarm_id}/resolve", response_model=AlarmOut)
def resolve_alarm(alarm_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    alarm = db.get(Alarm, alarm_id)
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")
    alarm.status = "resolved"
    alarm.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alarm)
    return alarm


# ---------- Alarm rules ----------
@router.get("/rules", response_model=list[AlarmRuleOut])
def list_rules(db: Session = Depends(get_db), device_id: Optional[int] = None):
    stmt = select(AlarmRule)
    if device_id:
        stmt = stmt.where(AlarmRule.device_id == device_id)
    return db.scalars(stmt).all()


@router.post("/rules", response_model=AlarmRuleOut, status_code=201)
def create_rule(payload: AlarmRuleCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rule = AlarmRule(**payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


# ---------- 报警 -> AI诊断 / 全链路 ----------
@router.post("/{alarm_id}/diagnose", response_model=DiagnosisOut)
def diagnose_alarm(alarm_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """对这条报警(重新)做 AI 诊断: 取报警发生前 24h 的有效读数(剔除 bad 质量)、规则包、知识库、SOP、
    历史维修复盘校准; 结果绑定到报警, 并刷新该报警上尚未完成的工单。"""
    from app.services import diagnosis_flow
    from app.services.alarms_hooks import refresh_open_work_order

    alarm = db.get(Alarm, alarm_id)
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")
    if not alarm.component_name:
        raise HTTPException(status_code=422, detail="Alarm has no component_name; nothing to diagnose")
    diagnosis = diagnosis_flow.diagnose_alarm(db, alarm, source="alarm_manual", user_id=user.id)
    refresh_open_work_order(db, alarm, diagnosis)
    db.commit()
    db.refresh(diagnosis)
    return diagnosis


@router.get("/{alarm_id}/chain")
def alarm_chain(alarm_id: int, db: Session = Depends(get_db)) -> dict:
    """这条报警的全链路视图: 设备→TAG→通讯→实时数据→数据质量→报警→AI诊断→知识库→工单→维修结果→AI反馈,
    每一环的当前状态 + 明确指出哪一环是断的(break_points)。"""
    from app.services.chain_trace import build_chain

    alarm = db.get(Alarm, alarm_id)
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")
    return build_chain(db, alarm)
