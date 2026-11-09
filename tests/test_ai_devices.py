"""Integration tests for AI Center's cross-menu device search / 选型 / context
endpoints and the chat binding to them (see app/api/v1/ai_devices.py and
app/services/device_search.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alarm import Alarm
from app.models.device import Device, DeviceComponent, DeviceModel, Gateway
from app.models.maintenance import Asset, LabAICertification, LabSample, MaintenanceRecord, WorkOrder
from app.models.measurement import Measurement

API = "/api/v1/ai/devices"


@pytest.fixture()
def fleet(db_session):
    """Three devices spanning two models, with data in every menu that matters:
    - SRCH-GC1   GC-2000  online   healthy, N2/CH4/CO2, gateway-bound, recent data
    - SRCH-FT1   FTIR-500 warning  H2S/CO2, 1 active alarm + 1 open work order + asset + lab sample
    - SRCH-OFF1  GC-2000  offline  H2S tag under the alias name 'H2S', no data
    """
    db = db_session
    gc = DeviceModel(model_name="GC-2000", manufacturer="AnalyzerCo", analyzer_type="GC")
    ft = DeviceModel(model_name="FTIR-500", manufacturer="AnalyzerCo", analyzer_type="FTIR")
    gw = Gateway(gateway_code="GW-SRCH", name="Search GW", protocol="MQTT", status="online")
    db.add_all([gc, ft, gw])
    db.flush()

    d1 = Device(device_code="SRCH-GC1", name="Line 1 chromatograph", model_id=gc.id, protocol="Modbus TCP",
                location="Unit 1 - Rack A", status="online", health_score=96)
    d2 = Device(device_code="SRCH-FT1", name="Line 2 FTIR", model_id=ft.id, protocol="OPC UA",
                location="Unit 2 - Rack B", status="warning", health_score=82)
    d3 = Device(device_code="SRCH-OFF1", name="Spare chromatograph", model_id=gc.id, protocol="Modbus TCP",
                location="Warehouse", status="offline", health_score=55)
    db.add_all([d1, d2, d3])
    db.flush()

    for name in ("Nitrogen", "Methane", "Carbon Dioxide"):
        db.add(DeviceComponent(device_id=d1.id, component_name=name, gateway_id=gw.id))
    for name in ("Hydrogen Sulfide", "Carbon Dioxide"):
        db.add(DeviceComponent(device_id=d2.id, component_name=name, gateway_id=gw.id))
    db.add(DeviceComponent(device_id=d3.id, component_name="H2S"))

    now = datetime.now(timezone.utc)
    for i in range(6):
        t = now - timedelta(hours=i)
        for name in ("Nitrogen", "Methane", "Carbon Dioxide"):
            db.add(Measurement(time=t, device_id=d1.id, component_name=name, value=10.0 + i))
        for name in ("Hydrogen Sulfide", "Carbon Dioxide"):
            db.add(Measurement(time=t, device_id=d2.id, component_name=name, value=5.0 + i))

    db.add(Alarm(device_id=d2.id, message="H2S high", severity="warning", status="active"))
    db.add(WorkOrder(device_id=d2.id, title="Check H2S cell", status="open"))
    db.add(Asset(asset_code="AST-SRCH", device_id=d2.id, name="FTIR asset"))
    db.add(LabSample(device_id=d2.id, component_name="Hydrogen Sulfide", lab_value=5.1, online_value=5.0))
    db.flush()
    return {"gc": d1, "ft": d2, "off": d3}


def codes(items):
    return [i["device_code"] for i in items]


# ------------------------------------------------------------- search ----
def test_search_by_code(client, fleet):
    body = client.get(f"{API}/search", params={"q": "SRCH-FT1"}).json()
    assert codes(body["items"]) == ["SRCH-FT1"] and body["total"] == 1
    hit = body["items"][0]
    assert hit["model_name"] == "FTIR-500" and hit["analyzer_type"] == "FTIR"
    assert sorted(hit["components"]) == ["Carbon Dioxide", "Hydrogen Sulfide"]
    assert hit["gateways"] == ["GW-SRCH"] and hit["asset_code"] == "AST-SRCH"
    assert hit["active_alarms"] == 1 and hit["open_work_orders"] == 1 and hit["lab_samples"] == 1
    assert hit["points_24h"] == 12


def test_search_natural_language_and_parsed_echo(client, fleet):
    body = client.get(f"{API}/search", params={"q": "在线 GC 甲烷"}).json()
    assert codes(body["items"]) == ["SRCH-GC1"]
    assert body["parsed"]["statuses"] == ["online"]
    assert body["parsed"]["analyzer_types"] == ["GC"]
    assert body["parsed"]["components"] == ["Methane"]


def test_search_component_alias_spans_differently_named_tags(client, fleet):
    # FT1's tag is "Hydrogen Sulfide", OFF1's tag is "H2S" — the query word is 硫化氢.
    body = client.get(f"{API}/search", params={"q": "硫化氢"}).json()
    assert set(codes(body["items"])) == {"SRCH-FT1", "SRCH-OFF1"}


def test_search_explicit_filter_params(client, fleet):
    body = client.get(f"{API}/search", params={"status": "offline", "component": "h2s"}).json()
    assert codes(body["items"]) == ["SRCH-OFF1"]
    body = client.get(f"{API}/search", params={"analyzer_type": "ftir"}).json()
    assert codes(body["items"]) == ["SRCH-FT1"]
    body = client.get(f"{API}/search", params={"protocol": "opc ua"}).json()
    assert codes(body["items"]) == ["SRCH-FT1"]


def test_search_empty_query_lists_all_and_limit(client, fleet):
    body = client.get(f"{API}/search", params={"limit": 2}).json()
    assert body["total"] >= 3 and len(body["items"]) == 2
    assert codes(body["items"]) == sorted(codes(body["items"]))


def test_search_no_match(client, fleet):
    body = client.get(f"{API}/search", params={"q": "definitely-not-a-device"}).json()
    assert body["total"] == 0 and body["items"] == []


def test_search_limit_validation(client):
    assert client.get(f"{API}/search", params={"limit": 0}).status_code == 422
    assert client.get(f"{API}/search", params={"limit": 101}).status_code == 422


# ------------------------------------------------------------- 选型 ----
def test_select_ranks_full_coverage_first(client, fleet):
    body = client.post(f"{API}/select", json={"query": "需要测 H2S 和 CO2 的设备", "limit": 10}).json()
    assert set(body["requirement"]["components"]) == {"Hydrogen Sulfide", "Carbon Dioxide"}
    top = body["devices"][0]
    assert top["device_code"] == "SRCH-FT1" and top["fit"] == "full" and top["missing_components"] == []
    assert top["score"] > 0 and top["reasons"]
    for r in top["reasons"]:
        assert r["zh"] and r["en"]
    by = {d["device_code"]: d for d in body["devices"]}
    assert by["SRCH-GC1"]["fit"] == "partial" and by["SRCH-GC1"]["missing_components"] == ["Hydrogen Sulfide"]
    # the alarm / work order on FT1 show up as reasons but don't override full coverage
    assert {"alarms", "work_orders"} <= {r["code"] for r in top["reasons"] if not r["ok"]}


def test_select_reports_model_level_evidence(client, fleet):
    body = client.post(f"{API}/select", json={"components": ["H2S"], "limit": 5}).json()
    models = {m["model_name"]: m for m in body["models"]}
    assert set(models) == {"GC-2000", "FTIR-500"}
    assert models["GC-2000"]["units"] == 2 and models["GC-2000"]["fit"] == "full"  # via SRCH-OFF1's H2S tag
    assert "SRCH-GC1" in models["GC-2000"]["device_codes"]
    assert body["basis_zh"] and body["basis_en"]


def test_select_online_only_and_min_health(client, fleet):
    body = client.post(f"{API}/select", json={"online_only": True}).json()
    assert {d["device_code"] for d in body["devices"]} == {"SRCH-GC1"}
    assert body["considered"] >= 1 and body["total_devices"] >= 3
    body = client.post(f"{API}/select", json={"min_health": 90}).json()
    assert "SRCH-FT1" not in {d["device_code"] for d in body["devices"]}


def test_select_prefers_requested_type(client, fleet):
    body = client.post(f"{API}/select", json={"analyzer_type": "FTIR", "limit": 10}).json()
    assert body["devices"][0]["device_code"] == "SRCH-FT1"
    assert body["models"][0]["model_name"] == "FTIR-500"


def test_select_validation(client):
    assert client.post(f"{API}/select", json={"min_health": 101}).status_code == 422
    assert client.post(f"{API}/select", json={"limit": 21}).status_code == 422


# ------------------------------------------------- context & facets ----
def test_device_context_links_every_menu(client, fleet):
    body = client.get(f"{API}/{fleet['ft'].id}/context").json()
    assert body["device"]["device_code"] == "SRCH-FT1"
    links = {l["menu"]: l for l in body["links"]}
    assert set(links) == {"dev", "comm", "danal", "dash", "maint", "asset", "lab", "cert", "roi"}
    assert links["comm"]["count"] == 2 and links["comm"]["detail"] == "GW-SRCH"
    assert links["danal"]["count"] == 12
    assert links["dash"]["count"] == 1 and links["maint"]["count"] == 1
    assert links["asset"]["detail"] == "AST-SRCH" and links["lab"]["count"] == 1
    # no AI certification and no purchase value on this fixture's asset -> no evidence, not a penalty
    assert links["cert"]["count"] == 0 and links["cert"]["detail"] is None
    assert links["roi"]["count"] == 0 and links["roi"]["detail"] is None


def test_device_context_404(client):
    assert client.get(f"{API}/999999/context").status_code == 404


# ----------------------------------------- certification & cost-health evidence ----
def test_select_uses_ai_certification_and_cost_health_evidence(client, fleet, db_session):
    """/ai/devices/select must reflect real, persisted AI-certification results (lab_ai_certifications)
    and real cost-side ROI flags (from the device's own asset + maintenance history) — this is the
    binding between AI Studio (SOP/ROI/Lab certification) and AI selection scoring, not a re-derivation."""
    db = db_session
    ft = fleet["ft"]  # SRCH-FT1: covers Hydrogen Sulfide + Carbon Dioxide, currently "partial" fit's rival
    # A certified H2S result for FT1 — evidence a plain profile lookup wouldn't have.
    db.add(LabAICertification(device_id=ft.id, component_name="Hydrogen Sulfide", status="certified", score=100.0,
                              window_minutes=30, sample_count=8, pair_count=8, r_squared=0.999))
    # A heavily used, cheap, old asset on FT1 -> should trip cost-health flags (high maintenance ratio etc.)
    db.add(Asset(asset_code="AST-FT1-EXTRA", device_id=ft.id, name="dup asset check", purchase_value=20000,
                 depreciation_years=5, purchase_date=datetime.now(timezone.utc) - timedelta(days=365 * 6)))
    # Heavy use: a repair whose cost alone is most of the asset's purchase value -> trips
    # "high_maintenance_ratio" (the near-end-of-life depreciation alone wouldn't).
    db.add(MaintenanceRecord(device_id=ft.id, action_taken="repair", cost=15000,
                             performed_at=datetime.now(timezone.utc) - timedelta(days=10)))
    db.flush()

    body = client.post(f"{API}/select", json={"components": ["H2S"], "limit": 10}).json()
    top = next(d for d in body["devices"] if d["device_code"] == "SRCH-FT1")
    codes = {r["code"]: r["ok"] for r in top["reasons"]}
    assert codes.get("certification") is True, top["reasons"]
    assert "certified" in next(r for r in top["reasons"] if r["code"] == "certification")["en"]
    assert codes.get("cost_health") is False, top["reasons"]
    assert codes.get("near_end_of_life") is True, top["reasons"]


def test_device_links_reflect_certification_and_cost_flags(client, fleet, db_session):
    db = db_session
    ft = fleet["ft"]
    db.add(LabAICertification(device_id=ft.id, component_name="Carbon Dioxide", status="failed", score=10.0,
                              window_minutes=30, sample_count=5, pair_count=5))
    db.add(MaintenanceRecord(device_id=ft.id, action_taken="repair", cost=15000, performed_at=datetime.now(timezone.utc) - timedelta(days=10)))
    db.add(Asset(asset_code="AST-FT1-2", device_id=ft.id, name="a2", purchase_value=20000, depreciation_years=5,
                 purchase_date=datetime.now(timezone.utc) - timedelta(days=365 * 6)))
    db.flush()

    links = {l["menu"]: l for l in client.get(f"{API}/{ft.id}/context").json()["links"]}
    assert links["cert"]["count"] == 1 and links["cert"]["detail"] == "Carbon Dioxide:failed"
    assert "high_maintenance_ratio" in links["roi"]["detail"] and links["roi"]["count"] >= 1


def test_facets(client, fleet):
    body = client.get(f"{API}/facets").json()
    assert "Hydrogen Sulfide" in body["components"] and "H2S" not in body["components"]
    assert {"GC", "FTIR"} <= set(body["analyzer_types"])
    assert {"GC-2000", "FTIR-500"} <= set(body["models"])
    assert {"online", "warning", "offline"} <= set(body["statuses"])


# ------------------------------------------------------------- chat ----
def test_chat_scoped_to_selected_device(client, fleet):
    # No device named in the text — the picked device_id carries the scope.
    resp = client.post("/api/v1/ai/chat", json={"message": "它现在怎么样？", "device_id": fleet["ft"].id})
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert "SRCH-FT1" in answer
    assert "H2S high" in answer  # active alarm pulled in without any alarm keyword
    assert "Check H2S cell" in answer  # open work order too


def test_chat_ignores_unknown_device_id(client, fleet):
    resp = client.post("/api/v1/ai/chat", json={"message": "现在设备情况怎么样？", "device_id": 999999})
    assert resp.status_code == 200, resp.text
    assert "设备总数" in resp.json()["answer"]


def test_chat_answers_selection_questions_from_the_engine(client, fleet):
    resp = client.post("/api/v1/ai/chat", json={"message": "推荐一台能测硫化氢和二氧化碳的设备"})
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert "设备选型结果" in answer and "SRCH-FT1" in answer


def test_chat_search_question_lists_matching_devices(client, fleet):
    resp = client.post("/api/v1/ai/chat", json={"message": "帮我找出离线的设备"})
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert "设备检索结果" in answer and "SRCH-OFF1" in answer
