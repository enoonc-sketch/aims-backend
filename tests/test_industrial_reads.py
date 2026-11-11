"""阶段 7：industrial 影子域补齐只读端点（原来 6 张表只写不读）。"""
import pytest


ENDPOINTS = [
    "/api/v1/industrial/connections",
    "/api/v1/industrial/tag-mappings",
    "/api/v1/industrial/acquisition-events",
    "/api/v1/industrial/quality-events",
    "/api/v1/industrial/models",
    "/api/v1/industrial/ai-feedback",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_read_endpoint_returns_paged_shape(client, path):
    r = client.get(path)
    assert r.status_code == 200
    body = r.json()
    assert "total" in body and isinstance(body["items"], list)


def test_connection_roundtrip(client):
    from app.models.device import Gateway
    db_session = None
    gw = client.get("/api/v1/gateways").json()
    gw_id = gw[0]["id"] if isinstance(gw, list) and gw else None
    if gw_id is None:
        pytest.skip("no gateway in fixture")
    r = client.post("/api/v1/industrial/connections", json={
        "gateway_id": gw_id, "name": "PLC-1", "protocol": "Modbus TCP",
        "endpoint": "192.168.1.10", "port": 502,
    })
    assert r.status_code == 200, r.text
    listed = client.get("/api/v1/industrial/connections").json()
    assert listed["total"] >= 1
    assert any(x.get("name") == "PLC-1" for x in listed["items"])


def test_acquisition_event_roundtrip(client, db_session):
    from app.models.device import Device
    from app.models.industrial import DeviceTag

    dev = Device(device_code="IND-1", name="ind")
    db_session.add(dev)
    db_session.commit()
    tag = DeviceTag(device_id=dev.id, tag_code="AT-101.CH4", name="CH4", data_type="float")
    db_session.add(tag)
    db_session.commit()

    r = client.post("/api/v1/industrial/acquisition-events", json={
        "tag_id": tag.id, "raw_value": "12.5", "engineering_value": 12.5, "quality_code": "good",
    })
    assert r.status_code in (200, 201), r.text
    listed = client.get(f"/api/v1/industrial/acquisition-events?device_id={dev.id}").json()
    assert listed["total"] >= 1
    assert listed["items"][0]["tag_id"] == tag.id


def test_read_endpoints_require_auth(anon_client):
    for path in ENDPOINTS:
        assert anon_client.get(path).status_code == 401
