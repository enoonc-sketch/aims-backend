"""Integration tests for AI Studio: AI-generated SOP / ROI, Data Analysis reading, knowledge Q&A,
diagnosis references, and the Data-Analysis-driven Lab comparison + AI certification.

No LLM is configured in the test environment, so every endpoint runs on its offline template
(`generated_by == "template"`), which is exactly the path that must work without a model."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.alarm import Alarm
from app.models.device import Device, DeviceComponent, DeviceModel
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.maintenance import (
    Asset,
    LabAICertification,
    LabSample,
    MaintenanceRecord,
    MaintenanceSOP,
    SparePart,
    WorkOrder,
)
from app.models.measurement import Measurement

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def studio(db_session):
    db = db_session
    model = DeviceModel(model_name="GC-STUDIO", manufacturer="AnalyzerCo", analyzer_type="GC")
    db.add(model)
    db.flush()
    dev = Device(device_code="STU-GC1", name="Studio GC", model_id=model.id, location="Unit 9", status="online", health_score=90)
    bare = Device(device_code="STU-BARE", name="No asset", status="online", health_score=80)
    db.add_all([dev, bare])
    db.flush()
    for c in ("Nitrogen", "Methane"):
        db.add(DeviceComponent(device_id=dev.id, component_name=c))

    db.add(MaintenanceSOP(device_model_id=model.id, sop_type="Calibration", title="GC-STUDIO calibration", content="1. Zero gas\n2. Span gas 50 ppm, tolerance ±2%", version="2.0"))
    db.add(SparePart(part_code="STU-P1", name="Carrier filter", device_model_id=model.id, stock_qty=0))
    db.add(Alarm(device_id=dev.id, component_name="Nitrogen", severity="warning", message="Nitrogen drift high", status="active"))
    db.add(WorkOrder(device_id=dev.id, title="Inspect cell", order_type="inspection", priority="high", status="open"))
    db.add(WorkOrder(device_id=dev.id, title="Fix valve", order_type="repair", status="closed"))
    db.add(Asset(asset_code="STU-AST", device_id=dev.id, name="Studio GC asset", purchase_value=120000, depreciation_years=10,
                 purchase_date=date.today() - timedelta(days=730)))
    db.add(MaintenanceRecord(device_id=dev.id, action_taken="Replaced filter", cost=4000, performed_at=NOW - timedelta(days=30)))
    db.add(MaintenanceRecord(device_id=dev.id, action_taken="Ancient repair", cost=99999, performed_at=NOW - timedelta(days=900)))

    doc = KnowledgeDocument(title="Calibration gas guide", doc_type="manual", status="ready")
    db.add(doc)
    db.flush()
    db.add(KnowledgeChunk(document_id=doc.id, chunk_index=0, content="Calibration gas must be certified and checked for expiry before every calibration of the chromatograph."))
    db.flush()
    return {"dev": dev, "bare": bare, "model": model}


def fill_measurements(db, dev_id, comp, values, start, step_min=10):
    for i, v in enumerate(values):
        db.add(Measurement(time=start + timedelta(minutes=step_min * i), device_id=dev_id, component_name=comp, value=v))
    db.flush()


# ------------------------------------------------------------------ SOP ----
def test_sop_generate_is_a_grounded_draft(client, studio):
    r = client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "calibration", "component_name": "Nitrogen", "lang": "en"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "draft" and b["generated_by"] == "template" and b["sop_type"] == "Calibration" and b["saved_sop_id"] is None
    assert b["device_model_id"] == studio["model"].id
    text = b["content"]
    assert "DRAFT" in text and "STU-GC1" in text and "Span gas 50 ppm, tolerance ±2%" in text     # vetted SOP content kept verbatim
    assert "Nitrogen drift high" in text and "Inspect cell" in text and "STU-P1" in text and "out of stock" in text
    kinds = {s["type"] for s in b["sources"]}
    assert "sop" in kinds
    assert b["context"]["has_vetted_sop"] is True and b["context"]["open_alarms"] == 1 and b["context"]["spare_parts"] == 1


def test_sop_generate_validation(client, studio):
    assert client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "nonsense"}).status_code == 400
    assert client.post(f"{API}/ai/sop/generate", json={"device_id": 999999, "sop_type": "Calibration"}).status_code == 404
    assert client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "Calibration", "lang": "fr"}).status_code == 422


def test_sop_without_a_vetted_procedure_says_the_steps_are_generic(client, studio):
    b = client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "Replacement", "lang": "en"}).json()
    assert "GENERIC" in b["content"] and b["context"]["has_vetted_sop"] is False


def test_saved_ai_draft_never_displaces_the_vetted_sop(client, studio):
    r = client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "Calibration", "save": True}).json()
    assert r["saved_sop_id"] is not None
    row = client.get(f"{API}/maintenance/sop", params={"device_model_id": studio["model"].id, "sop_type": "Calibration"}).json()
    assert {x["version"] for x in row} == {"2.0", "AI-draft"}
    looked_up = client.get(f"{API}/maintenance/sop/generate", params={"device_id": studio["dev"].id, "sop_type": "Calibration"}).json()
    assert looked_up["version"] == "2.0"
    # and a second AI generation must not build on the previous AI draft
    again = client.post(f"{API}/ai/sop/generate", json={"device_id": studio["dev"].id, "sop_type": "Calibration", "lang": "en"}).json()
    assert again["sources"][0]["title"] == "GC-STUDIO calibration"


# ------------------------------------------------------------------ ROI ----
def test_roi_numbers_come_from_platform_data(client, studio):
    b = client.post(f"{API}/ai/roi/generate", json={"device_id": studio["dev"].id, "period_months": 12, "annual_benefit": 40000, "lang": "en"}).json()
    m = b["metrics"]
    assert b["asset_code"] == "STU-AST" and b["generated_by"] == "template"
    assert m["purchase_value"] == 120000 and m["maintenance_cost"] == 4000 and m["maintenance_records"] == 1     # the 900-day-old record is outside the window
    assert m["work_orders"] == 2 and m["corrective_orders"] == 1 and m["preventive_orders"] == 1
    assert m["monthly_depreciation"] == 1000 and m["depreciation_in_period"] == 12000
    assert m["roi_pct"] == pytest.approx((40000 - 16000) / 16000 * 100, abs=0.1)
    assert "no_benefit" not in {g["code"] for g in b["data_gaps"]}
    assert "150.0%" in b["narrative"]


def test_roi_without_benefit_reports_cost_side_only(client, studio):
    b = client.post(f"{API}/ai/roi/generate", json={"device_id": studio["dev"].id}).json()
    assert "roi_pct" not in b["metrics"] and "payback_months" not in b["metrics"]
    assert "no_benefit" in {g["code"] for g in b["data_gaps"]}


def test_roi_for_a_device_without_an_asset_does_not_invent_figures(client, studio):
    b = client.post(f"{API}/ai/roi/generate", json={"device_id": studio["bare"].id, "annual_benefit": 5000}).json()
    assert b["asset_code"] is None and "purchase_value" not in b["metrics"] and "roi_pct" not in b["metrics"]
    assert {"no_asset", "roi_needs_asset"} <= {g["code"] for g in b["data_gaps"]}


def test_roi_validation(client, studio):
    assert client.post(f"{API}/ai/roi/generate", json={"device_id": 999999}).status_code == 404
    assert client.post(f"{API}/ai/roi/generate", json={"device_id": studio["dev"].id, "period_months": 0}).status_code == 422
    assert client.post(f"{API}/ai/roi/generate", json={"device_id": studio["dev"].id, "annual_benefit": -1}).status_code == 422


# ------------------------------------------------------- analysis reading ----
def test_analysis_read_server_data(client, studio, db_session):
    d = studio["dev"]
    start = NOW - timedelta(hours=5)
    fill_measurements(db_session, d.id, "Nitrogen", [100.0 + i for i in range(30)], start, step_min=5)
    fill_measurements(db_session, d.id, "Methane", [50.0] * 30, start, step_min=5)
    b = client.post(f"{API}/ai/analysis/read", json={"device_id": d.id, "hours": 24, "lang": "en"}).json()
    assert b["source"] == "server" and b["device_id"] == d.id and b["generated_by"] == "template"
    trend = [f for f in b["findings"] if f["code"] == "trend"]
    assert trend and trend[0]["series"] == "Nitrogen" and "rising" in trend[0]["en"]
    assert any(f["code"] == "flatline" and f["series"] == "Methane" for f in b["findings"])
    assert {s["name"] for s in b["series"]} == {"Nitrogen", "Methane"}


def test_analysis_read_inline_series_and_sum_check(client, studio):
    b = client.post(f"{API}/ai/analysis/read", json={"series": {"A": [60.0] * 10, "B": [30.0] * 10}, "check_sum": True}).json()
    assert b["source"] == "inline" and b["sum_check"]["within_range_pct"] == 0.0
    assert next(f for f in b["findings"] if f["code"] == "component_sum")["severity"] == "critical"


def test_analysis_read_validation(client, studio):
    assert client.post(f"{API}/ai/analysis/read", json={}).status_code == 400
    assert client.post(f"{API}/ai/analysis/read", json={"series": {}}).status_code == 400
    assert client.post(f"{API}/ai/analysis/read", json={"series": {"x": [1.0] * 5001}}).status_code == 400
    assert client.post(f"{API}/ai/analysis/read", json={"device_id": 999999}).status_code == 404
    assert client.post(f"{API}/ai/analysis/read", json={"device_id": studio["bare"].id}).status_code == 400   # no TAGs to read


# ------------------------------------------------------------ knowledge ----
def test_knowledge_ask_cites_its_sources(client, studio):
    b = client.post(f"{API}/ai/knowledge/ask", json={"question": "calibration gas expiry", "lang": "en"}).json()
    assert b["grounded"] is True and b["generated_by"] == "extractive"
    assert b["sources"] and b["sources"][0]["title"] == "Calibration gas guide" and b["sources"][0]["document_id"]
    assert "[1] Calibration gas guide" in b["answer"]


def test_knowledge_ask_admits_when_nothing_matches(client, studio):
    b = client.post(f"{API}/ai/knowledge/ask", json={"question": "zzzz qqqq xxxx", "lang": "en"}).json()
    assert b["grounded"] is False and b["generated_by"] == "none" and b["sources"] == [] and "Nothing relevant" in b["answer"]


def test_knowledge_ask_with_a_device_uses_live_data(client, studio):
    b = client.post(f"{API}/ai/knowledge/ask", json={"question": "what is happening with this analyzer", "device_id": studio["dev"].id, "lang": "en"}).json()
    assert b["live_context_used"] is True and b["grounded"] is True and "STU-GC1" in b["answer"]
    assert client.post(f"{API}/ai/knowledge/ask", json={"question": "x", "device_id": 999999}).status_code == 404
    assert client.post(f"{API}/ai/knowledge/ask", json={"question": ""}).status_code == 422


def test_diagnosis_now_carries_knowledge_references(client, studio, db_session):
    d = studio["dev"]
    fill_measurements(db_session, d.id, "Nitrogen", [10.0 + 0.5 * i for i in range(20)], NOW - timedelta(hours=3), step_min=5)
    r = client.post(f"{API}/ai/diagnose", json={"device_id": d.id, "component_name": "Nitrogen"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["references"], list)
    for ref in body["references"]:
        assert {"document_id", "document_title", "chunk_id", "score", "excerpt"} <= set(ref)
    assert body["diagnosis"] and body["recommendation"]     # existing behaviour unchanged


# ------------------------------------------------------------------- Lab ----
def add_lab(db, dev_id, comp, n, fn, base=None):
    """n lab samples one day apart; the analyzer's own measurement data around each = fn(lab)."""
    base = base or NOW - timedelta(days=n + 1)
    for i in range(n):
        lab = 10.0 + i
        t = base + timedelta(days=i)
        db.add(LabSample(device_id=dev_id, component_name=comp, sample_no=f"S{i}", lab_value=lab, online_value=None, sampled_at=t))
        for m in (-10, 0, 10):
            db.add(Measurement(time=t + timedelta(minutes=m), device_id=dev_id, component_name=comp, value=fn(i, lab)))
    db.flush()


def test_lab_compare_takes_the_online_value_from_analysis_data(client, studio, db_session):
    add_lab(db_session, studio["dev"].id, "Nitrogen", 8, lambda i, lab: lab + 0.5)      # online_value column left empty on purpose
    b = client.post(f"{API}/lab/analysis-compare", json={"device_id": studio["dev"].id, "component_name": "Nitrogen", "window_minutes": 30}).json()
    assert b["source"] == "analysis" and b["sample_count"] == 8 and len(b["pairs"]) == 8 and b["unmatched"] == []
    assert all(p["n_points"] == 3 and p["online"] == pytest.approx(p["lab"] + 0.5) for p in b["pairs"])
    assert b["stats"]["bias"] == pytest.approx(0.5) and b["stats"]["r_squared"] == pytest.approx(1.0)


def test_lab_compare_reports_samples_without_online_data(client, studio, db_session):
    d = studio["dev"]
    add_lab(db_session, d.id, "Nitrogen", 5, lambda i, lab: lab)
    db_session.add(LabSample(device_id=d.id, component_name="Nitrogen", sample_no="ORPHAN", lab_value=99.0, sampled_at=NOW - timedelta(days=400)))
    db_session.flush()
    b = client.post(f"{API}/lab/analysis-compare", json={"device_id": d.id, "component_name": "Nitrogen"}).json()
    assert len(b["pairs"]) == 5 and [u["reason"] for u in b["unmatched"]] == ["no_data_in_window"] and b["unmatched"][0]["sample_no"] == "ORPHAN"


def test_certify_certified_persists_and_refreshes_the_correlation_table(client, studio, db_session):
    d = studio["dev"]
    noise = [0.02, -0.02, 0.01, -0.01, 0.02, -0.02, 0.0, 0.01, -0.01, 0.0]
    add_lab(db_session, d.id, "Nitrogen", 10, lambda i, lab: lab + noise[i])
    r = client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Nitrogen", "lang": "en", "valid_days": 14})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "certified" and b["score"] == 100.0 and b["generated_by"] == "template" and b["certification_id"]
    assert b["valid_until"] is not None and "CNAS" in b["disclaimer"]["zh"] and all(c["ok"] for c in b["checks"])
    row = db_session.get(LabAICertification, b["certification_id"])
    assert row.status == "certified" and row.pair_count == 10 and row.device_id == d.id and row.checks and row.criteria["min_r2"] == 0.95
    corr = client.get(f"{API}/lab/correlations", params={"device_id": d.id}).json()      # the pre-existing endpoint now reflects analysis-based data
    assert len(corr) == 1 and corr[0]["sample_count"] == 10 and corr[0]["r_squared"] > 0.999


def test_certify_failed_conditional_and_insufficient(client, studio, db_session):
    d = studio["dev"]
    add_lab(db_session, d.id, "Nitrogen", 10, lambda i, lab: lab * (1.3 if i % 2 else 0.7))
    body = {"device_id": d.id, "component_name": "Nitrogen"}
    failed = client.post(f"{API}/lab/certify", json=body).json()
    assert failed["status"] == "failed" and failed["valid_until"] is None
    add_lab(db_session, d.id, "Methane", 3, lambda i, lab: lab)
    few = client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Methane"}).json()
    assert few["status"] == "insufficient" and few["valid_until"] is None and len(few["checks"]) == 1
    add_lab(db_session, d.id, "Oxygen", 10, lambda i, lab: lab * 1.04)
    cond = client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Oxygen", "criteria": {"max_mape_pct": 5, "max_abs_bias_pct": 3}}).json()
    assert cond["status"] == "conditional" and cond["valid_until"] is not None


def test_certify_can_skip_persistence_and_validates_criteria(client, studio, db_session):
    d = studio["dev"]
    add_lab(db_session, d.id, "Nitrogen", 6, lambda i, lab: lab)
    b = client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Nitrogen", "persist": False}).json()
    assert b["certification_id"] is None and client.get(f"{API}/lab/certifications", params={"device_id": d.id}).json() == []
    assert client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Nitrogen", "criteria": {"min_r3": 1}}).status_code == 400
    assert client.post(f"{API}/lab/certify", json={"device_id": 999999, "component_name": "x"}).status_code == 404
    assert client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Nitrogen", "window_minutes": 0}).status_code == 422


def test_certification_history_and_latest_overview(client, studio, db_session):
    d = studio["dev"]
    add_lab(db_session, d.id, "Nitrogen", 8, lambda i, lab: lab)
    for _ in range(2):
        assert client.post(f"{API}/lab/certify", json={"device_id": d.id, "component_name": "Nitrogen"}).status_code == 200
    everything = client.get(f"{API}/lab/certifications", params={"device_id": d.id}).json()
    assert len(everything) == 2 and everything[0]["id"] > everything[1]["id"] and everything[0]["device_code"] == "STU-GC1"
    latest = client.get(f"{API}/lab/certifications", params={"latest": True}).json()
    assert [x["id"] for x in latest if x["device_id"] == d.id] == [everything[0]["id"]]
    assert latest[0]["expired"] is False and latest[0]["status"] == "certified" and "CNAS" in latest[0]["disclaimer"]["zh"]
    # an expired certificate is flagged
    row = db_session.get(LabAICertification, everything[0]["id"])
    row.valid_until = NOW - timedelta(days=1)
    db_session.flush()
    assert client.get(f"{API}/lab/certifications", params={"device_id": d.id}).json()[0]["expired"] is True
