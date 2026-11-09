import io

from openpyxl import Workbook


def _create_device(client, code="Analyzer-IMP1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()


def test_import_csv_wide_format(client):
    device = _create_device(client)
    csv_bytes = (
        b"Time,Nitrogen,Methane\n"
        b"2026-09-18 00:00:00,40.1,85.2\n"
        b"2026-09-18 01:00:00,40.5,85.0\n"
    )
    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": device["id"]},
        files={"file": ("upload.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["format"] == "wide"
    assert body["rows_inserted"] == 4  # 2 rows x 2 components
    assert set(body["columns_detected"]) == {"Nitrogen", "Methane"}

    q = client.get(
        "/api/v1/measurements",
        params={"device_id": device["id"], "component_name": "Nitrogen", "hours": 24 * 365},
    )
    assert len(q.json()) == 2


def test_import_csv_long_format(client):
    device = _create_device(client, "Analyzer-IMP2")
    csv_bytes = (
        b"Time,Component,Value\n"
        b"2026-09-18 00:00:00,Ethane,5.3\n"
        b"2026-09-18 00:00:00,Nitrogen,40.1\n"
    )
    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": device["id"]},
        files={"file": ("upload.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["format"] == "long"
    assert body["rows_inserted"] == 2


def test_import_xlsx(client):
    device = _create_device(client, "Analyzer-IMP3")
    wb = Workbook()
    ws = wb.active
    ws.append(["Time", "CO2"])
    ws.append(["2026-09-18 00:00:00", 1.2])
    ws.append(["2026-09-18 01:00:00", 1.4])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": device["id"]},
        files={
            "file": (
                "upload.xlsx",
                buf,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["rows_inserted"] == 2


def test_import_unsupported_extension_400(client):
    device = _create_device(client, "Analyzer-IMP4")
    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": device["id"]},
        files={"file": ("upload.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 400


def test_import_unknown_device_404(client):
    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": 999999},
        files={"file": ("upload.csv", io.BytesIO(b"Time,X\n2026-01-01,1\n"), "text/csv")},
    )
    assert resp.status_code == 404


def test_import_bad_timestamp_column_400(client):
    device = _create_device(client, "Analyzer-IMP5")
    resp = client.post(
        "/api/v1/measurements/import",
        data={"device_id": device["id"]},
        files={"file": ("upload.csv", io.BytesIO(b"Foo,Bar\na,b\n"), "text/csv")},
    )
    assert resp.status_code == 400
