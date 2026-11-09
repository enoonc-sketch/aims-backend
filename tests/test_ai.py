def _create_device_with_data(client, code="Analyzer-AI1"):
    device = client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()
    for v in (40.0, 41.5, 42.0, 44.0):
        client.post(
            "/api/v1/measurements",
            json={"device_id": device["id"], "component_name": "Nitrogen", "value": v},
        )
    return device


def test_diagnose(client):
    device = _create_device_with_data(client)
    resp = client.post(
        "/api/v1/ai/diagnose",
        json={"device_id": device["id"], "component_name": "Nitrogen", "symptom": "Drift"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symptom"] == "Drift"
    assert 0 <= body["confidence"] <= 100
    assert body["diagnosis"]


def test_predict(client):
    device = _create_device_with_data(client, "Analyzer-AI2")
    resp = client.post(
        "/api/v1/ai/predict",
        json={"device_id": device["id"], "component_name": "Nitrogen", "horizon_hours": 6},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["predicted_values"]) == 6


def test_health_score(client):
    device = _create_device_with_data(client, "Analyzer-AI3")
    resp = client.get(f"/api/v1/ai/health/{device['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert 0 <= body["score"] <= 100


def test_chat_offline_mode(client):
    resp = client.post("/api/v1/ai/chat", json={"message": "What is the health of Analyzer-001?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"]
    assert "answer" in body


def test_diagnose_auto_detects_symptom_when_omitted(client):
    device = client.post(
        "/api/v1/devices", json={"device_code": "Analyzer-AI4", "name": "Analyzer-AI4"}
    ).json()
    # clear upward drift, no explicit symptom given
    for v in (40.0, 40.5, 41.0, 44.0, 47.0, 50.0, 53.0, 56.0):
        client.post(
            "/api/v1/measurements",
            json={"device_id": device["id"], "component_name": "Nitrogen", "value": v},
        )
    resp = client.post(
        "/api/v1/ai/diagnose",
        json={"device_id": device["id"], "component_name": "Nitrogen"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symptom_auto_detected"] is True
    assert "drift" in body["symptom"].lower()


def test_predict_includes_trend_and_bounds(client):
    device = _create_device_with_data(client, "Analyzer-AI5")
    resp = client.post(
        "/api/v1/ai/predict",
        json={"device_id": device["id"], "component_name": "Nitrogen", "horizon_hours": 4},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trend"] in ("rising", "falling", "stable")
    assert "lower" in body["predicted_values"][0]


def test_diagnose_unknown_device_404(client):
    resp = client.post(
        "/api/v1/ai/diagnose",
        json={"device_id": 999999, "component_name": "Nitrogen", "symptom": "Noise"},
    )
    assert resp.status_code == 404
