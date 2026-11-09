def _create_device(client, code="Analyzer-M1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()


def test_ingest_and_query_measurement(client):
    device = _create_device(client)

    resp = client.post(
        "/api/v1/measurements",
        json={"device_id": device["id"], "component_name": "Nitrogen", "value": 42.5},
    )
    assert resp.status_code == 201
    assert resp.json()["component_name"] == "Nitrogen"

    resp = client.get(
        "/api/v1/measurements", params={"device_id": device["id"], "component_name": "Nitrogen", "hours": 1}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["value"] == 42.5


def test_ingest_batch(client):
    device = _create_device(client, "Analyzer-M2")
    payload = {
        "device_id": device["id"],
        "readings": [
            {"device_id": device["id"], "component_name": "Methane", "value": 25.1},
            {"device_id": device["id"], "component_name": "Methane", "value": 25.4},
            {"device_id": device["id"], "component_name": "Ethylene", "value": 5.2},
        ],
    }
    resp = client.post("/api/v1/measurements/batch", json=payload)
    assert resp.status_code == 201
    assert resp.json()["inserted"] == 3


def test_trend_endpoint(client):
    device = _create_device(client, "Analyzer-M3")
    for v in (10.0, 11.0, 12.0):
        client.post(
            "/api/v1/measurements",
            json={"device_id": device["id"], "component_name": "CO2", "value": v},
        )
    resp = client.get(
        "/api/v1/measurements/trend",
        params={"device_id": device["id"], "components": ["CO2"], "hours": 24},
    )
    assert resp.status_code == 200
    series = resp.json()
    assert series[0]["component_name"] == "CO2"
    assert len(series[0]["points"]) == 3


def test_ingest_measurement_unknown_device_404(client):
    resp = client.post(
        "/api/v1/measurements", json={"device_id": 999999, "component_name": "Nitrogen", "value": 1}
    )
    assert resp.status_code == 404
