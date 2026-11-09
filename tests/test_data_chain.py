import io


def _create_device(client, code="Analyzer-CHAIN1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": f"{code} 分析仪"}).json()


def _create_gateway(client, code="GW-CHAIN1"):
    return client.post(
        "/api/v1/gateways", json={"gateway_code": code, "name": f"{code} 网关"}
    ).json()


def test_tag_import_creates_tag_and_links_gateway(client):
    device = _create_device(client)
    gateway = _create_gateway(client)
    csv_bytes = (
        f"设备编号,位号,网关编号,单位,下限,上限,地址\n"
        f"{device['device_code']},Methane,{gateway['gateway_code']},%,0,100,40001\n"
    ).encode("utf-8")

    resp = client.post(
        "/api/v1/devices/tags/import",
        files={"file": ("tags.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inserted"] == 1

    comps = client.get(f"/api/v1/devices/{device['id']}/components").json()
    assert len(comps) == 1
    tag = comps[0]
    assert tag["component_name"] == "Methane"
    assert tag["gateway_code"] == gateway["gateway_code"]
    assert tag["tag_address"] == "40001"
    assert tag["low_limit"] == 0
    assert tag["high_limit"] == 100


def test_tag_import_requires_existing_device(client):
    csv_bytes = b"device_code,tag_name\nNO-SUCH-DEVICE,Methane\n"
    resp = client.post(
        "/api/v1/devices/tags/import",
        files={"file": ("tags.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["inserted"] == 0
    assert body["skipped"] == 1


def test_measurement_ingest_creates_tag_automatically(client):
    device = _create_device(client, "Analyzer-CHAIN2")
    resp = client.post(
        "/api/v1/measurements",
        json={"device_id": device["id"], "component_name": "Ethane", "value": 12.3},
    )
    assert resp.status_code == 201
    comps = client.get(f"/api/v1/devices/{device['id']}/components").json()
    assert any(c["component_name"] == "Ethane" for c in comps)


def test_measurement_breach_creates_alarm_and_work_order(client):
    device = _create_device(client, "Analyzer-CHAIN3")
    # configure a threshold rule for this device+component
    rule = client.post(
        "/api/v1/alarms/rules",
        json={
            "device_id": device["id"],
            "component_name": "Nitrogen",
            "rule_type": "threshold",
            "high_limit": 50,
            "severity": "warning",
        },
    ).json()

    resp = client.post(
        "/api/v1/measurements",
        json={"device_id": device["id"], "component_name": "Nitrogen", "value": 75.0},
    )
    assert resp.status_code == 201

    alarms = client.get("/api/v1/alarms", params={"device_id": device["id"]}).json()
    assert len(alarms) == 1
    assert alarms[0]["rule_id"] == rule["id"]
    assert alarms[0]["status"] == "active"

    work_orders = client.get("/api/v1/maintenance/work-orders", params={"device_id": device["id"]}).json()
    assert len(work_orders) == 1
    assert work_orders[0]["alarm_id"] == alarms[0]["id"]
    assert work_orders[0]["priority"] == "medium"  # warning -> medium


def test_repeated_breach_does_not_duplicate_alarm(client):
    device = _create_device(client, "Analyzer-CHAIN4")
    client.post(
        "/api/v1/alarms/rules",
        json={
            "device_id": device["id"],
            "component_name": "Nitrogen",
            "rule_type": "threshold",
            "high_limit": 50,
            "severity": "critical",
        },
    )
    for v in (60.0, 65.0, 70.0):
        client.post(
            "/api/v1/measurements",
            json={"device_id": device["id"], "component_name": "Nitrogen", "value": v},
        )
    alarms = client.get("/api/v1/alarms", params={"device_id": device["id"]}).json()
    assert len(alarms) == 1  # only the first breach created an alarm

    work_orders = client.get("/api/v1/maintenance/work-orders", params={"device_id": device["id"]}).json()
    assert len(work_orders) == 1
    assert work_orders[0]["priority"] == "urgent"  # critical -> urgent


def test_analysis_import_triggers_alarm_chain(client):
    device = _create_device(client, "Analyzer-CHAIN5")
    client.post(
        "/api/v1/alarms/rules",
        json={
            "device_id": device["id"],
            "component_name": "Methane",
            "rule_type": "threshold",
            "high_limit": 50,
            "severity": "warning",
        },
    )
    csv_bytes = b"Time,Methane\n2026-09-19 00:00:00,90.0\n2026-09-19 01:00:00,95.0\n"
    resp = client.post(
        "/api/v1/analysis/import",
        data={"device_id": device["id"]},
        files={"file": ("an.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text

    alarms = client.get("/api/v1/alarms", params={"device_id": device["id"]}).json()
    assert len(alarms) == 1

    comps = client.get(f"/api/v1/devices/{device['id']}/components").json()
    assert any(c["component_name"] == "Methane" for c in comps)


def test_ai_chat_device_section_shows_tags(client):
    device = _create_device(client, "Analyzer-CHAIN6")
    gateway = _create_gateway(client, "GW-CHAIN6")
    csv_bytes = (
        f"设备编号,位号,网关编号\n{device['device_code']},Ethane,{gateway['gateway_code']}\n"
    ).encode("utf-8")
    client.post(
        "/api/v1/devices/tags/import",
        files={"file": ("tags.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    resp = client.post(
        "/api/v1/ai/chat",
        json={"message": f"设备 {device['device_code']} 的情况"},
    )
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert "Ethane" in answer
    assert gateway["gateway_code"] in answer
