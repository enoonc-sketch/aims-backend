"""一条报警的全链路视图:

    设备 → TAG → 通讯 → 实时数据 → 数据质量 → 报警 → AI诊断 → 知识库 → 工单 → 维修结果 → AI反馈

`build_chain` 把这十一个环节各自"现在是什么状态"放在一个响应里, 并明确指出**哪一环是断的**
(break_points): 例如 TAG 没绑网关所以无法追溯通讯、报警没有诊断、工单做完了但没评价 AI 诊断。
前端用它画链路; 运维用它一眼看出这条报警的闭环走到哪。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis, AIDiagnosisFeedback
from app.models.alarm import Alarm, AlarmRule
from app.models.device import Device, DeviceComponent, Gateway
from app.models.maintenance import MaintenanceRecord, MaintenanceSOP, WorkOrder
from app.models.measurement import Measurement

STAGES = (
    "device", "tag", "communication", "realtime_data", "data_quality", "alarm",
    "ai_diagnosis", "knowledge_base", "work_order", "repair_result", "ai_feedback",
)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def post_repair_check(db: Session, alarm: Alarm, wo: WorkOrder) -> Optional[dict]:
    """维修之后数据的客观表现: 修完之后有没有新数据、数据质量如何、是否又超限、是否又报警。
    (工程师的"修好了"是主观结论; 这是数据侧的旁证, 也是发现"修完又复发"的依据。)"""
    if wo.closed_at is None or not alarm.component_name:
        return None
    since = _aware(wo.closed_at)
    rows = db.execute(
        select(Measurement.value, Measurement.quality).where(
            Measurement.device_id == alarm.device_id,
            Measurement.component_name == alarm.component_name,
            Measurement.time > since,
        )
    ).all()
    usable = [v for v, q in rows if q != "bad"]
    rule = db.get(AlarmRule, alarm.rule_id) if alarm.rule_id else None
    breaches = 0
    if rule is not None:
        breaches = sum(
            1 for v in usable
            if (rule.low_limit is not None and v < float(rule.low_limit))
            or (rule.high_limit is not None and v > float(rule.high_limit))
        )
    recurred = db.scalar(
        select(Alarm.id).where(
            Alarm.device_id == alarm.device_id,
            Alarm.component_name == alarm.component_name,
            Alarm.alarm_type == alarm.alarm_type,
            Alarm.id != alarm.id,
            Alarm.triggered_at > since,
        ).order_by(Alarm.id).limit(1)
    )
    return {
        "since": _iso(since),
        "readings_after_repair": len(rows),
        "usable_readings": len(usable),
        "breaching_readings": breaches,
        "recurred_alarm_id": recurred,
        "verdict": (
            "no_data_yet" if not usable
            else "recurred" if recurred is not None or breaches
            else "stable"
        ),
    }


def build_chain(db: Session, alarm: Alarm) -> dict[str, Any]:
    device = db.get(Device, alarm.device_id)
    comp = alarm.component_name
    break_points: list[str] = []
    stage: dict[str, bool] = {k: False for k in STAGES}

    # 设备
    stage["device"] = device is not None
    device_out = (
        {"id": device.id, "device_code": device.device_code, "name": device.name,
         "status": device.status, "health_score": float(device.health_score or 0), "model_id": device.model_id}
        if device else None
    )

    # TAG
    tag = None
    if comp:
        tag = db.scalar(
            select(DeviceComponent).where(DeviceComponent.device_id == alarm.device_id, DeviceComponent.component_name == comp)
        )
    stage["tag"] = tag is not None
    tag_out = (
        {"id": tag.id, "component_name": tag.component_name, "unit": tag.unit, "tag_address": tag.tag_address,
         "low_limit": float(tag.low_limit) if tag.low_limit is not None else None,
         "high_limit": float(tag.high_limit) if tag.high_limit is not None else None,
         "gateway_id": tag.gateway_id}
        if tag else None
    )
    if tag is None:
        break_points.append("该组分没有对应的 TAG(设备→TAG 断开)")

    # 通讯
    gw = db.get(Gateway, tag.gateway_id) if tag is not None and tag.gateway_id else None
    stage["communication"] = gw is not None
    comm_out = (
        {"gateway_id": gw.id, "gateway_code": gw.gateway_code, "status": gw.status,
         "protocol": gw.protocol, "last_heartbeat": _iso(gw.last_heartbeat)}
        if gw else None
    )
    if tag is not None and gw is None:
        break_points.append("TAG 没有绑定网关(TAG→通讯 断开): 无法追溯这条数据是怎么采集上来的")
    elif gw is not None and not tag.tag_address:
        break_points.append("TAG 已绑定网关但没有 tag_address: 网关推送无法映射到这个 TAG")

    # 实时数据 + 数据质量
    latest, quality_24h = [], {"good": 0, "uncertain": 0, "bad": 0}
    if comp:
        rows = db.scalars(
            select(Measurement)
            .where(Measurement.device_id == alarm.device_id, Measurement.component_name == comp)
            .order_by(Measurement.time.desc()).limit(10)
        ).all()
        latest = [{"time": _iso(r.time), "value": r.value, "quality": r.quality, "import_id": r.import_id} for r in rows]
        end = _aware(alarm.triggered_at) or datetime.now(timezone.utc)
        for q, n in db.execute(
            select(Measurement.quality, func.count()).where(
                Measurement.device_id == alarm.device_id, Measurement.component_name == comp,
                Measurement.time <= end,
            ).group_by(Measurement.quality)
        ).all():
            quality_24h[q] = int(n)
    stage["realtime_data"] = bool(latest)
    stage["data_quality"] = bool(latest)
    if not latest:
        break_points.append("该点位在库里没有任何实时数据")
    if quality_24h.get("bad"):
        break_points.append(f"该点位存在 {quality_24h['bad']} 条 bad 质量读数(已被排除在报警与诊断证据之外)")

    # 报警
    stage["alarm"] = True
    alarm_out = {
        "id": alarm.id, "alarm_type": alarm.alarm_type, "severity": alarm.severity, "status": alarm.status,
        "message": alarm.message, "value": float(alarm.value) if alarm.value is not None else None,
        "rule_id": alarm.rule_id, "triggered_at": _iso(alarm.triggered_at), "resolved_at": _iso(alarm.resolved_at),
    }

    # AI 诊断 + 知识库
    diag = db.scalar(
        select(AIDiagnosis).where(AIDiagnosis.alarm_id == alarm.id).order_by(AIDiagnosis.id.desc()).limit(1)
    )
    stage["ai_diagnosis"] = diag is not None
    diag_out = None
    kb_out: dict[str, Any] = {"references": [], "sop": None}
    if diag is not None:
        ev = diag.evidence or {}
        diag_out = {
            "id": diag.id, "source": diag.source, "symptom": diag.symptom, "diagnosis": diag.diagnosis,
            "confidence": float(diag.confidence) if diag.confidence is not None else None,
            "recommendation": diag.recommendation, "created_at": _iso(diag.created_at),
            "evidence": {k: ev.get(k) for k in ("points", "bad_excluded", "window_hours", "anomaly", "feedback_note", "rule_id")},
        }
        kb_out = {"references": ev.get("knowledge") or [], "sop": ev.get("sop")}
    else:
        break_points.append("这条报警没有 AI 诊断(报警→AI诊断 断开); 可调 POST /alarms/{id}/diagnose")
    stage["knowledge_base"] = bool(kb_out["references"] or kb_out["sop"])
    if diag is not None and not stage["knowledge_base"]:
        break_points.append("诊断没有命中任何知识库内容或 SOP(AI诊断→知识库 没有产出): 知识库缺这一类故障的资料")

    # 工单
    wo = db.scalar(select(WorkOrder).where(WorkOrder.alarm_id == alarm.id).order_by(WorkOrder.id.desc()).limit(1))
    stage["work_order"] = wo is not None
    wo_out = None
    records_out: list[dict] = []
    repair_out: dict[str, Any] = {"records": records_out, "post_repair_check": None}
    fb_out = None
    if wo is None:
        if (alarm.severity or "").lower() in ("warning", "critical"):
            break_points.append("warning/critical 报警没有工单(报警→工单 断开)")
    else:
        wo_out = {
            "id": wo.id, "status": wo.status, "priority": wo.priority, "order_type": wo.order_type,
            "title": wo.title, "diagnosis_id": wo.diagnosis_id, "sop_id": wo.sop_id,
            "has_ai_brief": bool(wo.description), "closed_at": _iso(wo.closed_at),
        }
        if diag is not None and wo.diagnosis_id is None:
            break_points.append("工单没有携带 AI 诊断(诊断→工单 断开)")
        recs = db.scalars(
            select(MaintenanceRecord).where(MaintenanceRecord.work_order_id == wo.id).order_by(MaintenanceRecord.id)
        ).all()
        records_out.extend(
            {"id": r.id, "outcome": r.outcome, "action_taken": r.action_taken, "root_cause": r.root_cause,
             "parts_used": r.parts_used, "cost": float(r.cost) if r.cost is not None else None,
             "performed_at": _iso(r.performed_at)}
            for r in recs
        )
        stage["repair_result"] = any(r.outcome for r in recs)
        if wo.status in ("done", "closed") and not stage["repair_result"]:
            break_points.append("工单已关闭但没有维修结果(工单→维修结果 断开): 是用 close 直接关掉的")
        if wo.status in ("open", "in_progress"):
            break_points.append("工单尚未完成")
        repair_out["post_repair_check"] = post_repair_check(db, alarm, wo)

        fb = None
        if diag is not None:
            fb = db.scalar(
                select(AIDiagnosisFeedback)
                .where(AIDiagnosisFeedback.diagnosis_id == diag.id, AIDiagnosisFeedback.work_order_id == wo.id)
                .limit(1)
            )
        if fb is not None:
            fb_out = {
                "id": fb.id, "verdict": fb.verdict, "outcome": fb.outcome,
                "actual_root_cause": fb.actual_root_cause, "knowledge_document_id": fb.knowledge_document_id,
                "rated_at": _iso(fb.rated_at),
            }
            stage["ai_feedback"] = fb.verdict != "unrated"
            if fb.verdict == "unrated":
                break_points.append("维修结果已记录但还没评价 AI 诊断对不对(维修结果→AI反馈 未完成)")
        elif stage["repair_result"] and diag is not None:
            break_points.append("有维修结果但没有 AI 反馈记录")

    return {
        "alarm_id": alarm.id,
        "stages": stage,
        "closed_loop": all(stage[k] for k in STAGES),
        "break_points": break_points,
        "device": device_out,
        "tag": tag_out,
        "communication": comm_out,
        "realtime_data": {"latest": latest},
        "data_quality": quality_24h,
        "alarm": alarm_out,
        "ai_diagnosis": diag_out,
        "knowledge_base": kb_out,
        "work_order": wo_out,
        "repair_result": repair_out,
        "ai_feedback": fb_out,
    }
