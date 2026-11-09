"""报警 -> AI诊断 -> 知识库。

`/ai/diagnose` 原来是纯手动、且和报警毫无关联: AIDiagnosis 没有 alarm_id、诊断把坏质量数据也算进证据、
知识库引用只在响应里返回不落库、诊断从不知道过去的维修结果。这里把"给一条报警做诊断"抽成一个
同步、确定性(规则包 + 统计 + 可选已训练的异常模型 + 知识库 + SOP + 历史反馈校准)的服务:

- 报警产生时由 data_chain.on_alarm_created 自动调用(不依赖网络 / LLM, 入库路径不会因此变慢或失败)
- /alarms/{id}/diagnose 可重跑
- /ai/diagnose (手动, 带 LLM 润色) 复用同一个 build_draft / persist, 只是多一步 LLM

诊断依据(知识库摘录、建议 SOP、证据统计、校准说明)存进 AIDiagnosis.evidence, 工单据此生成描述。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis
from app.models.alarm import Alarm, AlarmRule
from app.models.device import Device
from app.models.maintenance import MaintenanceSOP
from app.models.measurement import Measurement
from app.services import repair_feedback
from app.services.ai_cores import match_diagnosis
from app.services.symptom_detect import detect_symptom

logger = logging.getLogger(__name__)

AI_DRAFT_VERSION = "AI-draft"  # 与 studio_data.AI_DRAFT_VERSION 一致: 不把 AI 草稿当作已审核 SOP 推荐
EVIDENCE_HOURS = 24
KB_LIMIT = 3

# 规则包(diagnosis_rules.json)是分析仪读数类的, 没有通讯/数据质量类故障, 这两类用内置模板。
_BUILTIN = {
    "comm": {
        "rule_id": "builtin_comm_loss",
        "confidence": 0.7,
        "diagnosis": "该点位长时间没有新数据: 采集通讯链路中断的可能性最大（网关掉电/断网、协议转换或采集程序停止、"
                     "tag 地址或从站号被改动），也可能是分析仪本身停止输出。",
        "recommendation": "1) 在通讯管理里对该网关/设备做连接测试，区分「网络不通」与「协议无响应」。"
                          "2) 现场检查网关供电、网线与交换机端口。3) 核对 TAG 的 tag_address 与设备侧寄存器/节点是否一致。"
                          "4) 若通讯正常但仍无数据，检查分析仪是否处于停机/校准/报警锁定状态。",
    },
    "quality": {
        "rule_id": "builtin_bad_quality",
        "confidence": 0.65,
        "diagnosis": "该点位连续收到质量为 bad 的读数: 设备/网关自己判定数据不可信（传感器故障、信号线路或采集配置异常），"
                     "在恢复前这些读数不参与工艺报警与诊断。",
        "recommendation": "1) 在设备本体查看该通道的故障/质量标志。2) 检查传感器与信号线路。"
                          "3) 核对采集配置（量程、缩放系数、地址）。4) 修复后观察质量是否恢复为 good。",
    },
}

_SOP_TYPE_BY_FAMILY = {"drift": "Calibration", "sum": "Calibration"}
_SOURCE_CN = {"alarm_auto": "报警自动诊断", "alarm_manual": "手动重跑", "manual": "手动诊断"}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- evidence ----
def gather_evidence(
    db: Session, device_id: int, component_name: str, *, as_of: Optional[datetime] = None, hours: int = EVIDENCE_HOURS
) -> tuple[list[float], int]:
    """(有效读数, 被剔除的 bad 读数条数)。bad 质量的读数不是证据。"""
    end = _aware(as_of) or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    base = (
        Measurement.device_id == device_id,
        Measurement.component_name == component_name,
        Measurement.time >= start,
        Measurement.time <= end,
    )
    values = db.scalars(
        select(Measurement.value).where(*base, Measurement.quality != "bad").order_by(Measurement.time.asc())
    ).all()
    bad = db.scalar(select(func.count()).select_from(Measurement).where(*base, Measurement.quality == "bad")) or 0
    return list(values), int(bad)


def _direction_symptom(db: Session, alarm: Alarm) -> str:
    rule = db.get(AlarmRule, alarm.rule_id) if alarm.rule_id else None
    if rule is None or alarm.value is None:
        return ""
    v = float(alarm.value)
    if rule.low_limit is not None and v < float(rule.low_limit):
        return "low_reading low"
    if rule.high_limit is not None and v > float(rule.high_limit):
        return "high_reading high"
    return ""


# -------------------------------------------------------------- knowledge ----
def kb_lookup(db: Session, query: str, limit: int = KB_LIMIT) -> list[dict]:
    """同步的知识库检索(关键词 / FTS)。永不抛异常: 索引坏了不能拖垮诊断。"""
    from app.services.rag import search_chunks
    from app.services.studio_data import excerpt

    try:
        hits = search_chunks(db, query, limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("knowledge lookup failed")
        return []
    return [
        {
            "document_id": c.document_id,
            "document_title": title,
            "chunk_id": c.id,
            "score": round(float(score), 4),
            "excerpt": excerpt(c.content),
        }
        for c, title, score in hits
        if score and float(score) > 0
    ]


def pick_sop(db: Session, device: Device, family: str) -> Optional[dict]:
    if not device.model_id:
        return None
    wanted = _SOP_TYPE_BY_FAMILY.get(family, "Maintenance")
    for sop_type in dict.fromkeys([wanted, "Maintenance"]):
        sop = db.scalar(
            select(MaintenanceSOP)
            .where(
                MaintenanceSOP.device_model_id == device.model_id,
                MaintenanceSOP.sop_type == sop_type,
                (MaintenanceSOP.version.is_(None)) | (MaintenanceSOP.version != AI_DRAFT_VERSION),
            )
            .order_by(MaintenanceSOP.id)
        )
        if sop is not None:
            return {"id": sop.id, "title": sop.title, "sop_type": sop.sop_type, "version": sop.version}
    return None


# ------------------------------------------------------------------ draft ----
def build_draft(
    db: Session,
    device: Device,
    component_name: str,
    *,
    symptom: Optional[str] = None,
    alarm: Optional[Alarm] = None,
    kb_hits: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """确定性地生成一份诊断草稿(尚未落库)。kb_hits 传入则不再检索(异步路径可预先用向量检索)。"""
    alarm_type = (alarm.alarm_type if alarm is not None else None) or "process"
    as_of = _aware(alarm.triggered_at) if alarm is not None else None
    recent, bad_excluded = gather_evidence(db, device.id, component_name, as_of=as_of)

    symptom_text = (symptom or "").strip()
    auto_detected = False
    builtin = _BUILTIN.get("comm" if alarm_type == "comm" else "quality" if alarm_type == "data_quality" else "")

    if builtin is not None:
        symptom_text = symptom_text or ("comm loss" if alarm_type == "comm" else "data quality bad")
        matched = dict(builtin)
    else:
        if not symptom_text:
            detected, _info = detect_symptom(recent)
            direction = _direction_symptom(db, alarm) if alarm is not None else ""
            merged = " ".join(dict.fromkeys(f"{detected or ''} {direction}".split()))
            symptom_text = merged or "unknown"
            auto_detected = bool(merged)
        matched = match_diagnosis(symptom_text, component_name)

    family = repair_feedback.symptom_family(symptom_text)
    confidence = float(matched["confidence"])
    diagnosis = matched["diagnosis"]
    recommendation = matched["recommendation"]
    if auto_detected:
        diagnosis = f"[Symptom auto-detected as '{symptom_text}' from recent data] " + diagnosis

    # 历史维修复盘校准置信度 (维修结果 -> AI反馈 回路)
    confidence, feedback_note = repair_feedback.calibrate_confidence(db, component_name, family, confidence)

    # 已训练的异常模型(若有)交叉验证 —— 只对读数类诊断有意义
    anomaly_info = None
    if builtin is None and recent:
        from app.services import anomaly

        res = anomaly.score(device.id, component_name, recent)
        if res:
            anomaly_info = {"is_anomaly": bool(res["is_anomaly"]), "anomaly_ratio": float(res["anomaly_ratio"])}
            if res["is_anomaly"]:
                confidence = min(0.97, confidence + 0.15)
                diagnosis += (
                    f" (Confirmed by trained anomaly model: {res['anomaly_ratio']*100:.0f}%"
                    " of recent readings are statistically unusual for this component.)"
                )
            else:
                confidence = max(0.15, confidence - 0.1)
                diagnosis += " (Note: trained anomaly model finds recent readings statistically normal for this component.)"

    if kb_hits is None:
        q = (
            f"{component_name} 通讯 网关 {symptom_text}" if alarm_type == "comm"
            else f"{component_name} 数据质量 传感器 {symptom_text}" if alarm_type == "data_quality"
            else f"{component_name} {symptom_text} analyzer"
        )
        kb_hits = kb_lookup(db, q)

    return {
        "device_id": device.id,
        "component_name": component_name,
        "symptom": symptom_text,
        "symptom_auto_detected": auto_detected,
        "diagnosis": diagnosis,
        "confidence": confidence,
        "recommendation": recommendation,
        "evidence": {
            "alarm_type": alarm_type,
            "rule_id": matched.get("rule_id"),
            "window_hours": EVIDENCE_HOURS,
            "points": len(recent),
            "bad_excluded": bad_excluded,
            "as_of": as_of.isoformat() if as_of else None,
            "anomaly": anomaly_info,
            "feedback_note": feedback_note,
            "knowledge": kb_hits,
            "sop": pick_sop(db, device, family),
        },
    }


def persist(
    db: Session, draft: dict[str, Any], *, alarm: Optional[Alarm], source: str, user_id: Optional[int] = None
) -> AIDiagnosis:
    row = AIDiagnosis(
        device_id=draft["device_id"],
        component_name=draft["component_name"],
        symptom=draft["symptom"],
        diagnosis=draft["diagnosis"],
        confidence=draft["confidence"],
        recommendation=draft["recommendation"],
        requested_by=user_id,
        alarm_id=alarm.id if alarm is not None else None,
        source=source,
        evidence=draft["evidence"],
    )
    db.add(row)
    db.flush()
    return row


def diagnose_alarm(
    db: Session, alarm: Alarm, *, source: str = "alarm_auto", user_id: Optional[int] = None,
    kb_hits: Optional[list[dict]] = None,
) -> Optional[AIDiagnosis]:
    device = db.get(Device, alarm.device_id)
    if device is None or not alarm.component_name:
        return None
    draft = build_draft(db, device, alarm.component_name, alarm=alarm, kb_hits=kb_hits)
    return persist(db, draft, alarm=alarm, source=source, user_id=user_id)


# ------------------------------------------------------------ work order -----
def work_order_brief(alarm: Optional[Alarm], diagnosis: AIDiagnosis) -> str:
    ev = diagnosis.evidence or {}
    lines = []
    if alarm is not None:
        lines.append(f"【报警 #{alarm.id}】{alarm.message}")
    lines.append(
        f"【AI诊断 #{diagnosis.id}｜{_SOURCE_CN.get(diagnosis.source or 'manual', diagnosis.source)}】"
        f"症状: {diagnosis.symptom}；置信度 {float(diagnosis.confidence or 0):.0%}"
    )
    lines.append(f"诊断: {diagnosis.diagnosis}")
    lines.append(f"建议: {diagnosis.recommendation}")
    basis = f"近 {ev.get('window_hours', EVIDENCE_HOURS)}h 有效读数 {ev.get('points', 0)} 条"
    if ev.get("bad_excluded"):
        basis += f"（已剔除 bad 质量 {ev['bad_excluded']} 条）"
    if ev.get("feedback_note"):
        basis += f"；{ev['feedback_note']}"
    lines.append(f"【诊断依据】{basis}")
    kb = ev.get("knowledge") or []
    if kb:
        lines.append("【知识库参考】")
        for i, h in enumerate(kb, 1):
            lines.append(f"{i}. 《{h.get('document_title') or '-'}》{h.get('excerpt') or ''}")
    sop = ev.get("sop")
    if sop:
        lines.append(f"【建议 SOP】《{sop['title']}》（{sop['sop_type']}，v{sop.get('version') or '-'}）")
    return "\n".join(lines)
