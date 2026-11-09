def _create_device(client, code="Analyzer-MT1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()


def test_work_order_crud_and_close(client):
    device = _create_device(client)
    resp = client.post(
        "/api/v1/maintenance/work-orders",
        json={"device_id": device["id"], "title": "Replace filter", "priority": "high"},
    )
    assert resp.status_code == 201
    wo = resp.json()
    assert wo["status"] == "open"

    resp = client.get("/api/v1/maintenance/work-orders", params={"device_id": device["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.patch(f"/api/v1/maintenance/work-orders/{wo['id']}", json={"status": "in_progress"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"

    resp = client.post(f"/api/v1/maintenance/work-orders/{wo['id']}/close")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["closed_at"] is not None


def test_work_order_unknown_device_404(client):
    resp = client.post(
        "/api/v1/maintenance/work-orders", json={"device_id": 999999, "title": "X"}
    )
    assert resp.status_code == 404


def test_maintenance_record_create_and_list(client):
    device = _create_device(client, "Analyzer-MT2")
    wo = client.post(
        "/api/v1/maintenance/work-orders", json={"device_id": device["id"], "title": "Calib check"}
    ).json()

    resp = client.post(
        "/api/v1/maintenance/records",
        json={
            "work_order_id": wo["id"],
            "device_id": device["id"],
            "action_taken": "Recalibrated with reference gas",
            "cost": 120.5,
        },
    )
    assert resp.status_code == 201

    resp = client.get("/api/v1/maintenance/records", params={"work_order_id": wo["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_sop_create_and_generate(client, db_session):
    from app.models.device import Device, DeviceModel

    model = DeviceModel(model_name="GC-TEST", manufacturer="TestCo", analyzer_type="GC")
    db_session.add(model)
    db_session.commit()
    db_session.refresh(model)

    device = Device(device_code="Analyzer-MT3", name="MT3", model_id=model.id)
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)

    resp = client.post(
        "/api/v1/maintenance/sop",
        json={
            "device_model_id": model.id,
            "sop_type": "Calibration",
            "title": "GC-TEST Calibration",
            "content": "Step 1...\nStep 2...",
        },
    )
    assert resp.status_code == 201

    resp = client.get(
        "/api/v1/maintenance/sop/generate",
        params={"device_id": device.id, "sop_type": "Calibration"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "GC-TEST Calibration"


def test_sop_generate_not_found(client):
    device = _create_device(client, "Analyzer-MT4")  # no model_id set
    resp = client.get(
        "/api/v1/maintenance/sop/generate",
        params={"device_id": device["id"], "sop_type": "Calibration"},
    )
    assert resp.status_code == 404


def test_spare_parts_crud_and_stock_adjust(client):
    resp = client.post(
        "/api/v1/maintenance/spare-parts",
        json={"part_code": "SP-001", "name": "Sample filter", "stock_qty": 10},
    )
    assert resp.status_code == 201
    part = resp.json()

    resp = client.post("/api/v1/maintenance/spare-parts", json={"part_code": "SP-001", "name": "dup"})
    assert resp.status_code == 409

    resp = client.post(f"/api/v1/maintenance/spare-parts/{part['id']}/adjust-stock", json={"delta": -3})
    assert resp.status_code == 200
    assert resp.json()["stock_qty"] == 7

    resp = client.post(f"/api/v1/maintenance/spare-parts/{part['id']}/adjust-stock", json={"delta": -100})
    assert resp.status_code == 400

    resp = client.get("/api/v1/maintenance/spare-parts", params={"low_stock_below": 8})
    assert resp.status_code == 200
    assert any(p["part_code"] == "SP-001" for p in resp.json())
