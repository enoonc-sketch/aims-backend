"""维修结果 -> AI反馈。

闭环的最后一环。之前工单 close 只改状态, 没有"修好了没有 / 真正根因是什么 / AI 诊断对不对"
这些信息, 所以 AI 永远不知道自己诊断得准不准。这里做三件事:

1. record_feedback: 工单完成时, 把 诊断 <-> 工单 <-> 维修记录 <-> 复盘结论 存成一条 AIDiagnosisFeedback。
   verdict(诊断对不对) 由工程师给出; 没给就是 unrated —— 系统**不替人猜**。
2. calibrate_confidence: 之后同一组分、同一类症状的诊断, 若已有 >= MIN_RATED 条已评价的复盘,
   就把规则包给的固定置信度和历史准确率混合。样本不足不校准, 并在诊断里说明样本量。
3. write_case_to_kb: 修复成功的案例(症状 / AI 诊断 / 实际根因 / 实际处理)写回知识库,
   下一次同类报警做知识库检索时就能命中真实的历史案例 —— 这是 维修结果 -> 知识库 的回路。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai import AIDiagnosis, AIDiagnosisFeedback
from app.models.device import Device, DeviceModel
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.maintenance import MaintenanceRecord, WorkOrder

logger = logging.getLogger(__name__)

VERDICTS = ("correct", "partially_correct", "incorrect")
OUTCOMES = ("fixed", "partially_fixed", "not_fixed", "no_fault_found")
MIN_RATED = 3           # 至少这么多条已评价复盘才校准置信度
BLEND = 0.5             # 校准后置信度 = (1-BLEND)*规则包置信度 + BLEND*历史准确率
CASE_DOC_TYPE = "case"

_VERDICT_CN = {"correct": "诊断正确", "partially_correct": "诊断部分正确", "incorrect": "诊断有误", "unrated": "未评价"}
_OUTCOME_CN = {"fixed": "已修复", "partially_fixed": "部分修复", "not_fixed": "未修复", "no_fault_found": "未发现故障"}


def symptom_family(symptom: Optional[str]) -> str:
    """把各种症状写法归到一类: 'drift high_reading high' / 'Drift' -> 'drift'。"""
    s = (symptom or "").lower()
    for key in ("comm", "quality", "drift", "noise", "spike", "response", "flame", "sum", "pressure"):
        if key in s:
            return "noise" if key == "spike" else key
    if "high" in s or "low" in s:
        return "level"
    return "other"


# ------------------------------------------------------------------ record --
def record_feedback(
    db: Session,
    *,
    diagnosis_id: int,
    work_order: Optional[WorkOrder],
    record: Optional[MaintenanceRecord],
    verdict: Optional[str],
    outcome: Optional[str],
    root_cause: Optional[str],
    action_taken: Optional[str],
) -> AIDiagnosisFeedback:
    """按 (诊断, 工单) 幂等地写入/更新复盘: 一张工单先"部分修复"再"修复"只会有一条反馈。"""
    wo_id = work_order.id if work_order is not None else None
    fb = db.scalar(
        select(AIDiagnosisFeedback).where(
            AIDiagnosisFeedback.diagnosis_id == diagnosis_id,
            AIDiagnosisFeedback.work_order_id == wo_id,
        )
    )
    if fb is None:
        fb = AIDiagnosisFeedback(diagnosis_id=diagnosis_id, work_order_id=wo_id, verdict="unrated")
        db.add(fb)
    if record is not None:
        fb.maintenance_record_id = record.id
    if outcome:
        fb.outcome = outcome
    if root_cause:
        fb.actual_root_cause = root_cause
    if action_taken:
        fb.actual_action = action_taken
    if verdict in VERDICTS:
        fb.verdict = verdict
        fb.rated_at = datetime.now(timezone.utc)
    db.flush()
    return fb


# --------------------------------------------------------------- calibrate --
def accuracy_stats(db: Session, component_name: str, family: Optional[str] = None) -> dict:
    rows = db.execute(
        select(AIDiagnosisFeedback.verdict, AIDiagnosis.symptom)
        .join(AIDiagnosis, AIDiagnosis.id == AIDiagnosisFeedback.diagnosis_id)
        .where(AIDiagnosis.component_name == component_name)
        .order_by(AIDiagnosisFeedback.id.desc())
        .limit(500)
    ).all()
    verdicts = [v for v, sym in rows if family is None or symptom_family(sym) == family]
    rated = [v for v in verdicts if v in VERDICTS]
    n = len(rated)
    correct = sum(1 for v in rated if v == "correct")
    partial = sum(1 for v in rated if v == "partially_correct")
    return {
        "component_name": component_name,
        "symptom_family": family,
        "total": len(verdicts),
        "rated": n,
        "unrated": len(verdicts) - n,
        "correct": correct,
        "partially_correct": partial,
        "incorrect": n - correct - partial,
        "accuracy": round((correct + 0.5 * partial) / n, 4) if n else None,
    }


def calibrate_confidence(db: Session, component_name: str, family: str, base: float) -> tuple[float, Optional[str]]:
    st = accuracy_stats(db, component_name, family)
    if st["total"] == 0:
        return base, None
    if st["rated"] < MIN_RATED:
        return base, f"该组分同类诊断已有 {st['total']} 次复盘、其中 {st['rated']} 次已评价，样本不足 {MIN_RATED} 次，置信度未校准"
    conf = max(0.1, min(0.97, (1 - BLEND) * base + BLEND * st["accuracy"]))
    note = (
        f"按 {st['rated']} 次已评价的维修复盘校准: 历史同类诊断准确率 {st['accuracy']:.0%}，"
        f"置信度 {base:.0%} → {conf:.0%}"
    )
    return conf, note


# ------------------------------------------------------------ KB write-back --
def write_case_to_kb(
    db: Session,
    fb: AIDiagnosisFeedback,
    diagnosis: AIDiagnosis,
    work_order: WorkOrder,
    record: MaintenanceRecord,
    *,
    user_id: Optional[int] = None,
) -> Optional[KnowledgeDocument]:
    """修复成功(或确认无故障)且给出了根因/处理措施的案例 -> 知识库文档 + 一个分块。

    嵌入向量此处不计算(向量化是异步的、且依赖本地模型); 分块靠关键词/FTS 检索即可命中,
    模型可用时在知识库页重新向量化即可。"""
    if record.outcome not in ("fixed", "no_fault_found"):
        return None
    if not (record.root_cause or record.action_taken):
        return None

    device = db.get(Device, work_order.device_id) if work_order.device_id else None
    model = db.get(DeviceModel, device.model_id) if device is not None and device.model_id else None
    comp = diagnosis.component_name or "-"
    content = "\n".join(
        [
            f"【维修案例】设备 {device.device_code if device else '-'}"
            f"（型号 {model.model_name if model else '-'}）组分 {comp}",
            f"症状: {diagnosis.symptom or '-'}",
            f"AI 诊断（置信度 {float(diagnosis.confidence or 0):.0%}）: {diagnosis.diagnosis or '-'}",
            f"AI 建议: {diagnosis.recommendation or '-'}",
            f"复盘: {_VERDICT_CN.get(fb.verdict, fb.verdict)}；维修结果: {_OUTCOME_CN.get(record.outcome, record.outcome)}",
            f"实际根因: {record.root_cause or '-'}",
            f"实际处理: {record.action_taken or '-'}",
            f"用料: {record.parts_used or '-'}",
        ]
    )

    doc = db.get(KnowledgeDocument, fb.knowledge_document_id) if fb.knowledge_document_id else None
    if doc is None:
        doc = KnowledgeDocument(
            title=f"维修案例 {device.device_code if device else '-'} {comp} #{work_order.id}",
            doc_type=CASE_DOC_TYPE,
            device_model_id=device.model_id if device else None,
            uploaded_by=user_id,
            status="ready",
        )
        db.add(doc)
        db.flush()
        db.add(KnowledgeChunk(document_id=doc.id, chunk_index=0, content=content))
        fb.knowledge_document_id = doc.id
    else:
        chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id, KnowledgeChunk.chunk_index == 0))
        if chunk is not None:
            chunk.content = content
            chunk.embedding = None  # 旧向量对应旧内容
        else:
            db.add(KnowledgeChunk(document_id=doc.id, chunk_index=0, content=content))
    db.flush()
    return doc
