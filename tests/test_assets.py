def _create_device(client, code="Analyzer-AS1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()


def test_asset_crud(client):
    device = _create_device(client)
    resp = client.post(
        "/api/v1/assets",
        json={
            "asset_code": "AST-001",
            "device_id": device["id"],
            "name": "Process Gas Analyzer 001",
            "category": "Analyzer",
            "purchase_value": 120000,
            "depreciation_years": 10,
        },
    )
    assert resp.status_code == 201
    asset = resp.json()
    assert asset["current_value"] == 120000

    resp = client.get(f"/api/v1/assets/{asset['id']}")
    assert resp.status_code == 200

    resp = client.patch(f"/api/v1/assets/{asset['id']}", json={"status": "idle"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"

    resp = client.get("/api/v1/assets", params={"q": "Process Gas"})
    assert resp.status_code == 200
    assert any(a["asset_code"] == "AST-001" for a in resp.json())


def test_duplicate_asset_code_rejected(client):
    payload = {"asset_code": "AST-DUP", "name": "Dup"}
    assert client.post("/api/v1/assets", json=payload).status_code == 201
    resp = client.post("/api/v1/assets", json=payload)
    assert resp.status_code == 409


def test_asset_roi_calculation(client):
    resp = client.post(
        "/api/v1/assets",
        json={
            "asset_code": "AST-ROI",
            "name": "ROI Test Asset",
            "purchase_value": 120000,
            "depreciation_years": 10,
        },
    )
    asset = resp.json()

    resp = client.get(f"/api/v1/assets/{asset['id']}/roi", params={"period_months": 12})
    assert resp.status_code == 200
    roi = resp.json()
    assert roi["purchase_value"] == 120000
    # 120000 / (10*12) = 1000/month -> 12 months = 12000 accumulated
    assert round(roi["accumulated_depreciation"], 2) == 12000.0
    assert round(roi["net_book_value"], 2) == 108000.0
    assert round(roi["depreciation_pct"], 2) == 10.0

    # persisted back onto the asset
    resp = client.get(f"/api/v1/assets/{asset['id']}")
    assert round(resp.json()["current_value"], 2) == 108000.0


def test_asset_roi_by_device(client):
    device = _create_device(client, "Analyzer-AS2")
    client.post(
        "/api/v1/assets",
        json={
            "asset_code": "AST-DEV",
            "device_id": device["id"],
            "name": "Device-linked asset",
            "purchase_value": 60000,
            "depreciation_years": 5,
        },
    )
    resp = client.get(f"/api/v1/assets/by-device/{device['id']}/roi", params={"period_months": 6})
    assert resp.status_code == 200
    assert resp.json()["asset_id"]


def test_asset_documents(client):
    resp = client.post("/api/v1/assets", json={"asset_code": "AST-DOC", "name": "Doc Asset"})
    asset = resp.json()

    resp = client.post(
        f"/api/v1/assets/{asset['id']}/documents",
        json={"doc_name": "manual.pdf", "doc_type": "manual", "file_path": "/files/manual.pdf"},
    )
    assert resp.status_code == 201

    resp = client.get(f"/api/v1/assets/{asset['id']}/documents")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_asset_not_found_404(client):
    assert client.get("/api/v1/assets/999999").status_code == 404
    assert client.get("/api/v1/assets/999999/roi").status_code == 404
