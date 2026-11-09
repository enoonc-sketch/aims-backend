from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_current_user, require_permission
from app.db.session import get_db
from app.models.industrial import CommunicationConnection, DeviceTag, TagMapping, AcquisitionEvent, DataQualityEvent, AIModelVersion, AIFeedback
from app.services.audit import record_audit
from app.models.device import Device
from app.models.maintenance import WorkOrder
from app.models.measurement import Measurement
from app.services import data_chain, repair_feedback
from app.services.data_quality import validate_measurement

router = APIRouter(prefix="/industrial", tags=["Industrial Core"])

def _dump(x):
    return {c.name:getattr(x,c.name) for c in x.__table__.columns}

@router.post("/connections", dependencies=[Depends(require_permission("communication.write"))])
def create_connection(payload: dict, db: Session=Depends(get_db), user=Depends(get_current_user)):
    x=CommunicationConnection(**payload); db.add(x); db.flush(); record_audit(db,user_id=user.id,action="COMMUNICATION_CONNECTION_CREATE",entity="communication_connections",entity_id=x.id); db.commit(); db.refresh(x); return _dump(x)

@router.post("/tags", dependencies=[Depends(require_permission("tag.write"))])
def create_tag(payload: dict, db: Session=Depends(get_db), user=Depends(get_current_user)):
    x=DeviceTag(**payload); db.add(x); db.flush(); record_audit(db,user_id=user.id,action="DEVICE_TAG_CREATE",entity="device_tags",entity_id=x.id); db.commit(); db.refresh(x); return _dump(x)

@router.post("/tag-mappings", dependencies=[Depends(require_permission("tag.write"))])
def create_mapping(payload: dict, db: Session=Depends(get_db), user=Depends(get_current_user)):
    x=TagMapping(**payload); db.add(x); db.flush(); record_audit(db,user_id=user.id,action="TAG_MAPPING_CREATE",entity="tag_mappings",entity_id=x.id); db.commit(); db.refresh(x); return _dump(x)

@router.post("/acquisition-events", dependencies=[Depends(require_permission("measurement.write"))])
def ingest_event(payload: dict, db: Session=Depends(get_db), user=Depends(get_current_user)):
    now=datetime.now(timezone.utc)
    payload.setdefault("event_time", now)
    for key in ("event_time", "source_timestamp"):
        if isinstance(payload.get(key), str):
            payload[key] = datetime.fromisoformat(payload[key].replace("Z", "+00:00"))
    x=AcquisitionEvent(**payload); db.add(x); db.flush()
    tag=db.get(DeviceTag, x.tag_id) if x.tag_id else None
    if tag and x.engineering_value is not None:
        issues=validate_measurement(value=x.engineering_value, quality=x.quality_code,
                                    low=float(tag.min_value) if tag.min_value is not None else None,
                                    high=float(tag.max_value) if tag.max_value is not None else None,
                                    source_timestamp=x.source_timestamp, received_at=x.received_at or now)
        device_id=tag.device_id
        for issue in issues:
            db.add(DataQualityEvent(measurement_time=x.event_time, device_id=device_id,
                                    component_name=tag.name, quality_code=x.quality_code,
                                    rule_code=issue["rule_code"], severity=issue["severity"],
                                    details=issue.get("details",{})))
    # Bridge into the closed loop: a usable event is also a Measurement, so it goes through the same
    # data_chain.process_readings as gateway pushes / file imports (quality -> alarm -> AI diagnosis
    # -> work order). Raw event + DataQualityEvent rows above stay as the acquisition-level record.
    chain = None
    if tag and x.engineering_value is not None:
        device = db.get(Device, tag.device_id)
        ts = x.source_timestamp or x.event_time
        exists = device and db.scalar(select(Measurement.device_id).where(
            Measurement.device_id == device.id, Measurement.component_name == tag.name, Measurement.time == ts))
        if device and not exists:
            q = (x.quality_code or "good").lower()
            row = Measurement(time=ts, device_id=device.id, component_name=tag.name,
                              value=float(x.engineering_value),
                              quality="bad" if q == "bad" else ("good" if q in ("good", "manual") else "uncertain"))
            out = data_chain.process_readings(db, device, [row], realtime=True)
            chain = {"quality": out.quality, "alarms": [a.id for a in out.alarms + out.dq_alarms],
                     "diagnoses": [d.id for d in out.diagnoses], "work_orders": [w.id for w in out.work_orders]}
    record_audit(db,user_id=user.id,action="ACQUISITION_EVENT_INGEST",entity="acquisition_events",entity_id=x.id,
                 detail={"tag_id":x.tag_id,"quality_code":x.quality_code,"chain":chain})
    db.commit(); db.refresh(x)
    return {**_dump(x), "chain": chain}

@router.get("/tags/{device_id}")
def list_tags(device_id:int, db:Session=Depends(get_db), user=Depends(get_current_user)):
    return [_dump(x) for x in db.scalars(select(DeviceTag).where(DeviceTag.device_id==device_id)).all()]

@router.post("/models", dependencies=[Depends(require_permission("ai.admin"))])
def register_model(payload:dict, db:Session=Depends(get_db), user=Depends(get_current_user)):
    x=AIModelVersion(**payload); db.add(x); db.flush()
    record_audit(db,user_id=user.id,action="AI_MODEL_REGISTER",entity="ai_model_versions",entity_id=x.id,
                 detail={"model_key":x.model_key,"version":x.version})
    db.commit(); db.refresh(x); return _dump(x)

@router.post("/ai-feedback", dependencies=[Depends(require_permission("ai.feedback"))])
def create_ai_feedback(payload:dict, db:Session=Depends(get_db), user=Depends(get_current_user)):
    payload.setdefault("verified_by", user.id); payload.setdefault("verified_at", datetime.now(timezone.utc))
    verdict = payload.pop("diagnosis_verdict", None)   # engineer's call; absent => stays "unrated" (system never guesses)
    x=AIFeedback(**payload); db.add(x); db.flush()
    loop_id = None
    if x.diagnosis_id:
        wo = db.get(WorkOrder, x.work_order_id) if x.work_order_id else None
        fb = repair_feedback.record_feedback(
            db, diagnosis_id=x.diagnosis_id, work_order=wo, record=None,
            verdict=verdict if verdict in repair_feedback.VERDICTS else None,
            outcome=x.outcome if x.outcome in repair_feedback.OUTCOMES else None,
            root_cause=x.root_cause, action_taken=x.corrective_action)
        loop_id = fb.id
    record_audit(db,user_id=user.id,action="AI_FEEDBACK_CREATE",entity="ai_feedback",entity_id=x.id,
                 detail={"diagnosis_id":x.diagnosis_id,"work_order_id":x.work_order_id,"loop_feedback_id":loop_id})
    db.commit(); db.refresh(x); return {**_dump(x), "loop_feedback_id": loop_id}
