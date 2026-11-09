"""AI 反馈 API — 维修结果 -> AI反馈 闭环的读写入口。

- 工单完成(POST /maintenance/work-orders/{id}/complete)会自动登记反馈;
- 这里让工程师事后补评价(诊断对不对)、看待评价清单、看某组分的诊断准确率;
- 已评价的反馈会校准之后同类诊断的置信度, 修复成功的案例会写回知识库(见 services/repair_feedback.py)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.ai import AIDiagnosis, AIDiagnosisFeedback
from app.models.maintenance import MaintenanceRecord, WorkOrder
from app.models.system import User
from app.schemas.ai import DiagnosisOut, FeedbackOut, FeedbackRateIn, FeedbackStats
from app.services import repair_feedback

router = APIRouter(prefix="/ai", tags=["AI Feedback"])


@router.get("/diagnoses", response_model=list[DiagnosisOut])
def list_diagnoses(
    db: Session = Depends(get_db),
    device_id: Optional[int] = None,
    alarm_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=500),
):
    stmt = select(AIDiagnosis)
    if device_id is not None:
        stmt = stmt.where(AIDiagnosis.device_id == device_id)
    if alarm_id is not None:
        stmt = stmt.where(AIDiagnosis.alarm_id == alarm_id)
    return db.scalars(stmt.order_by(AIDiagnosis.id.desc()).limit(limit)).all()


@router.get("/diagnoses/{diagnosis_id}", response_model=DiagnosisOut)
def get_diagnosis(diagnosis_id: int, db: Session = Depends(get_db)):
    row = db.get(AIDiagnosis, diagnosis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Diagnosis not found")
    return row


@router.post("/diagnoses/{diagnosis_id}/feedback", response_model=FeedbackOut)
def rate_diagnosis(
    diagnosis_id: int,
    payload: FeedbackRateIn,
    work_order_id: Optional[int] = Query(None, description="不填则评价这条诊断最近的一条复盘"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工程师评价 AI 诊断对不对(correct / partially_correct / incorrect), 可补充实际根因。"""
    if payload.verdict not in repair_feedback.VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {list(repair_feedback.VERDICTS)}")
    diagnosis = db.get(AIDiagnosis, diagnosis_id)
    if not diagnosis:
        raise HTTPException(status_code=404, detail="Diagnosis not found")

    stmt = select(AIDiagnosisFeedback).where(AIDiagnosisFeedback.diagnosis_id == diagnosis_id)
    if work_order_id is not None:
        stmt = stmt.where(AIDiagnosisFeedback.work_order_id == work_order_id)
    fb = db.scalar(stmt.order_by(AIDiagnosisFeedback.id.desc()).limit(1))
    wo = db.get(WorkOrder, fb.work_order_id) if fb is not None and fb.work_order_id else None
    if fb is None:
        wo = db.get(WorkOrder, work_order_id) if work_order_id is not None else None

    fb = repair_feedback.record_feedback(
        db, diagnosis_id=diagnosis_id, work_order=wo, record=None,
        verdict=payload.verdict, outcome=None, root_cause=payload.actual_root_cause, action_taken=None,
    )
    # 评价后, 已有的维修案例(若有)要把"诊断对不对"更新进去
    record = db.get(MaintenanceRecord, fb.maintenance_record_id) if fb.maintenance_record_id else None
    if wo is not None and record is not None:
        if payload.actual_root_cause:
            record.root_cause = payload.actual_root_cause
        repair_feedback.write_case_to_kb(db, fb, diagnosis, wo, record, user_id=user.id)
    db.commit()
    db.refresh(fb)
    return fb


@router.get("/feedback/pending", response_model=list[dict])
def pending_feedback(db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)):
    """已经有维修记录/已关闭、但还没人评价 AI 诊断对不对的工单。"""
    rows = db.execute(
        select(AIDiagnosisFeedback, AIDiagnosis)
        .join(AIDiagnosis, AIDiagnosis.id == AIDiagnosisFeedback.diagnosis_id)
        .where(AIDiagnosisFeedback.verdict == "unrated")
        .order_by(AIDiagnosisFeedback.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "feedback_id": fb.id,
            "diagnosis_id": d.id,
            "work_order_id": fb.work_order_id,
            "device_id": d.device_id,
            "component_name": d.component_name,
            "symptom": d.symptom,
            "ai_diagnosis": d.diagnosis,
            "outcome": fb.outcome,
            "actual_root_cause": fb.actual_root_cause,
        }
        for fb, d in rows
    ]


@router.get("/feedback/stats", response_model=FeedbackStats)
def feedback_stats(
    component_name: str = Query(...),
    symptom_family: Optional[str] = Query(None, description="drift / noise / level / comm / quality ..."),
    db: Session = Depends(get_db),
):
    """某组分(可按症状类别细分)的 AI 诊断历史准确率 —— 置信度校准用的就是这份统计。"""
    return repair_feedback.accuracy_stats(db, component_name, symptom_family)
