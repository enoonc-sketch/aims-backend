def test_dashboard_stats(client):
    client.post("/api/v1/devices", json={"device_code": "Analyzer-D1", "name": "D1", "status": "online"})
    client.post("/api/v1/devices", json={"device_code": "Analyzer-D2", "name": "D2", "status": "fault"})

    resp = client.get("/api/v1/dashboard/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_devices"] >= 2
    assert body["online_devices"] >= 1
    assert body["fault_devices"] >= 1


def test_health_overview(client):
    client.post(
        "/api/v1/devices",
        json={"device_code": "Analyzer-D3", "name": "D3", "status": "online", "health_score": 91.2},
    )
    resp = client.get("/api/v1/dashboard/health-overview")
    assert resp.status_code == 200
    codes = [d["device_code"] for d in resp.json()]
    assert "Analyzer-D3" in codes


def test_root_and_health_endpoints(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
