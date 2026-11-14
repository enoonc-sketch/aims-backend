"""Integration of the three lines merged in 0.5.0:
   chain loop (alarm -> diagnosis -> work order -> feedback)  +  industrial core (TAG/connection/acquisition)
   + security hardening. These tests pin the seams between them."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.models.ai import AIDiagnosisFeedback
from app.models.measurement import Measurement
from app.models.system import AuditLog

API = "/api/v1"


def _industrial_setup(client, code="MRG-1"):
    d = client.post(f"{API}/devices", json={"device_code": code, "name": code, "protocol": "Modbus TCP"}).json()
    client.post(f"{API}/alarms/rules",
                json={"device_id": d["id"], "component_name": "O2", "high_limit": 20, "severity": "warning"})
    tag = client.post(f"{API}/industrial/tags",
                      json={"device_id": d["id"], "tag_code": "O2", "name": "O2", "unit": "%", "min_value": 0, "max_value": 25}).json()
    return d, tag


def test_migrations_form_one_linear_chain():
    """007 industrial core, 008 chain loop and 009 security must not fork (both used to be '007')."""
    vdir = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    edges = {}
    for f in vdir.glob("*.py"):
        s = f.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?:: \w+(?:\[.*?\])?)? = "([^"]+)"', s, re.M).group(1)
        down = re.search(r'^down_revision(?:: [^=]+)? = (None|"[^"]+")', s, re.M).group(1).strip('"')
        edges[rev] = down
    assert len(edges) == len(set(edges)), "duplicate revision ids"
    children = {}
    for rev, down in edges.items():
        children.setdefault(down, []).append(rev)
    assert all(len(v) == 1 for v in children.values()), f"migration branch: {children}"
    order, cur = [], "None"
    while cur in children:
        cur = children[cur][0]
        order.append(cur)
    assert len(order) == len(edges), "unreachable revision"
    assert order[-3:] == ["008_chain_feedback_loop", "009_security_hardening", "010_must_change_password"]


def test_acquisition_event_feeds_the_alarm_diagnosis_workorder_chain(client, db_session):
    d, tag = _industrial_setup(client)
    r = client.post(f"{API}/industrial/acquisition-events", json={
        "tag_id": tag["id"], "engineering_value": 30, "raw_value": "3000", "quality_code": "good",
        "event_time": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 200, r.text
    chain = r.json()["chain"]
    assert chain and chain["alarms"] and chain["diagnoses"] and chain["work_orders"]
    assert db_session.scalar(select(Measurement).where(Measurement.device_id == d["id"], Measurement.component_name == "O2"))


def test_bad_quality_acquisition_event_is_recorded_but_does_not_raise_process_alarm(client, db_session):
    d, tag = _industrial_setup(client, "MRG-2")
    r = client.post(f"{API}/industrial/acquisition-events", json={
        "tag_id": tag["id"], "engineering_value": 99, "quality_code": "bad",
        "event_time": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 200
    assert r.json()["chain"]["alarms"] == []          # untrusted value must not open a process alarm


def test_duplicate_acquisition_event_does_not_duplicate_measurements(client, db_session):
    d, tag = _industrial_setup(client, "MRG-3")
    ts = datetime.now(timezone.utc).isoformat()
    for _ in range(2):
        client.post(f"{API}/industrial/acquisition-events",
                    json={"tag_id": tag["id"], "engineering_value": 5, "quality_code": "good", "event_time": ts})
    rows = db_session.scalars(select(Measurement).where(Measurement.device_id == d["id"])).all()
    assert len(rows) == 1


def test_industrial_ai_feedback_lands_in_the_loop_feedback_table(client, db_session):
    d, tag = _industrial_setup(client, "MRG-4")
    chain = client.post(f"{API}/industrial/acquisition-events", json={
        "tag_id": tag["id"], "engineering_value": 30, "quality_code": "good",
        "event_time": datetime.now(timezone.utc).isoformat()}).json()["chain"]
    diag_id = chain["diagnoses"][0]
    r = client.post(f"{API}/industrial/ai-feedback",
                    json={"diagnosis_id": diag_id, "outcome": "fixed", "root_cause": "sample line fouled",
                          "corrective_action": "replaced filter"})
    assert r.status_code == 200, r.text
    fb = db_session.scalar(select(AIDiagnosisFeedback).where(AIDiagnosisFeedback.diagnosis_id == diag_id))
    assert fb and fb.actual_root_cause == "sample line fouled" and fb.outcome == "fixed"
    assert fb.verdict == "unrated"                    # the system never guesses whether the AI was right
    assert r.json()["loop_feedback_id"] == fb.id


def test_engineer_verdict_is_carried_through(client, db_session):
    d, tag = _industrial_setup(client, "MRG-5")
    diag_id = client.post(f"{API}/industrial/acquisition-events", json={
        "tag_id": tag["id"], "engineering_value": 30, "quality_code": "good",
        "event_time": datetime.now(timezone.utc).isoformat()}).json()["chain"]["diagnoses"][0]
    client.post(f"{API}/industrial/ai-feedback", json={"diagnosis_id": diag_id, "outcome": "fixed", "diagnosis_verdict": "correct"})
    fb = db_session.scalar(select(AIDiagnosisFeedback).where(AIDiagnosisFeedback.diagnosis_id == diag_id))
    assert fb.verdict == "correct" and fb.rated_at is not None


def test_new_write_endpoints_require_auth_and_are_audited(anon_client, db_session):
    """chain's new writers (gateway ingest, stale scan, work-order completion, diagnosis rating)
    must be behind the hardened auth, and every denied attempt lands in the audit trail."""
    calls = [("post", f"{API}/communication/ingest", {"gateway_code": "X", "points": []}),
             ("post", f"{API}/communication/scan-stale", {}),
             ("post", f"{API}/maintenance/work-orders/1/complete", {"outcome": "fixed"}),
             ("post", f"{API}/ai/diagnoses/1/feedback", {"verdict": "correct"}),
             ("post", f"{API}/alarms/1/diagnose", {}),
             ("post", f"{API}/industrial/acquisition-events", {"tag_id": 1}),
             ("post", f"{API}/industrial/ai-feedback", {"outcome": "fixed"}),
             ("post", f"{API}/industrial/models", {"model_key": "m", "version": "1", "task": "t"})]
    for m, path, body in calls:
        assert getattr(anon_client, m)(path, json=body).status_code == 401, path
    denied = [a for a in db_session.scalars(select(AuditLog)).all() if a.action == "API_POST" and a.detail["outcome"] == "denied"]
    assert len(denied) >= len(calls)


def test_data_quality_module_exposes_both_apis():
    from app.services import data_quality as dq
    assert hasattr(dq, "assess_rows") and hasattr(dq, "ComponentAssessor")        # chain
    assert dq.validate_measurement(value=30, quality="good", low=0, high=25)[0]["rule_code"] == "OUT_OF_RANGE_HIGH"  # industrial


def test_scheduler_degrades_when_apscheduler_missing():
    src = (Path(__file__).resolve().parent.parent / "app/services/external_scheduler.py").read_text(encoding="utf-8")
    assert "APScheduler is not installed" in src
