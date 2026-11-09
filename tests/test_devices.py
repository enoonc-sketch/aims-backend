def test_create_and_get_device(client):
    payload = {
        "device_code": "Analyzer-TEST-01",
        "name": "Test Analyzer",
        "protocol": "Modbus TCP",
        "location": "Test Rack",
        "status": "online",
        "health_score": 98.5,
    }
    resp = client.post("/api/v1/devices", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["device_code"] == "Analyzer-TEST-01"
    device_id = body["id"]

    resp = client.get(f"/api/v1/devices/{device_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Analyzer"


def test_duplicate_device_code_rejected(client):
    payload = {"device_code": "Analyzer-DUP", "name": "Dup 1"}
    assert client.post("/api/v1/devices", json=payload).status_code == 201
    resp = client.post("/api/v1/devices", json=payload)
    assert resp.status_code == 409


def test_list_and_filter_devices(client):
    client.post("/api/v1/devices", json={"device_code": "Analyzer-A", "name": "A", "status": "online"})
    client.post("/api/v1/devices", json={"device_code": "Analyzer-B", "name": "B", "status": "fault"})

    resp = client.get("/api/v1/devices", params={"status": "fault"})
    assert resp.status_code == 200
    codes = [d["device_code"] for d in resp.json()]
    assert "Analyzer-B" in codes
    assert "Analyzer-A" not in codes


def test_update_device(client):
    created = client.post("/api/v1/devices", json={"device_code": "Analyzer-U", "name": "U"}).json()
    resp = client.patch(f"/api/v1/devices/{created['id']}", json={"status": "warning"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "warning"


def test_get_missing_device_404(client):
    resp = client.get("/api/v1/devices/999999")
    assert resp.status_code == 404
