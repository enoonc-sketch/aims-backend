import io

import pytest


def _create_device(client, code="Analyzer-MOD1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": f"{code} 分析仪"}).json()


# ---------------------------------------------------------------- devices --
def test_import_devices_creates_and_upserts(client):
    csv_bytes = (
        "设备编号,设备名称,型号,位置,状态\n"
        "DEV-A,色谱仪A,GC-2000,车间一,online\n"
        "DEV-B,色谱仪B,GC-2000,车间二,offline\n"
    ).encode("utf-8-sig")

    resp = client.post(
        "/api/v1/devices/import",
        files={"file": ("devices.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 2
    assert body["skipped"] == 0
    assert set(body["columns_detected"]) >= {"device_code", "name", "model_name", "location", "status"}

    listed = client.get("/api/v1/devices").json()
    codes = {d["device_code"] for d in listed}
    assert {"DEV-A", "DEV-B"} <= codes

    # re-import with an updated status for DEV-A -> upsert, not a duplicate
    csv_bytes2 = ("设备编号,设备名称,状态\nDEV-A,色谱仪A,fault\n").encode("utf-8-sig")
    resp2 = client.post(
        "/api/v1/devices/import",
        files={"file": ("devices2.csv", io.BytesIO(csv_bytes2), "text/csv")},
    )
    assert resp2.status_code == 201
    assert resp2.json()["updated"] == 1

    dev_a = next(d for d in client.get("/api/v1/devices").json() if d["device_code"] == "DEV-A")
    assert dev_a["status"] == "fault"


def test_import_devices_missing_required_columns(client):
    csv_bytes = b"foo,bar\n1,2\n"
    resp = client.post(
        "/api/v1/devices/import",
        files={"file": ("bad.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 400


def test_import_devices_legacy_xls_works(client):
    """Regression test: a legacy .xls upload used to raise an uncaught
    ImportError (missing xlrd) that surfaced as a bare HTTP 500. It must now
    either succeed (xlrd installed, as declared in requirements.txt) or fail
    with a clean 400 — never a 500."""
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    ws.write(0, 0, "device_code")
    ws.write(0, 1, "name")
    ws.write(1, 0, "XLS-DEV1")
    ws.write(1, 1, "Legacy XLS Device")
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = client.post(
        "/api/v1/devices/import",
        files={"file": ("legacy.xls", buf, "application/vnd.ms-excel")},
    )
    assert resp.status_code in (201, 400), resp.text
    if resp.status_code == 201:
        assert resp.json()["inserted"] == 1
    else:
        # acceptable only if it's a clean, informative 400 — never a 500
        assert "xlrd" in resp.json()["detail"].lower() or "excel" in resp.json()["detail"].lower()


def test_import_devices_corrupt_file_returns_400_not_500(client):
    """A file that claims to be .xlsx but isn't valid Excel content must
    return 400, not an unhandled 500."""
    resp = client.post(
        "/api/v1/devices/import",
        files={"file": ("fake.xlsx", io.BytesIO(b"this is not a real excel file"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_import_devices_malformed_csv_returns_400_not_500(client):
    """Ragged/malformed CSV (unterminated quote) must return 400, not 500."""
    bad_csv = b'device_code,name\nDEV-1,"unterminated\nDEV-2,Test\n'
    resp = client.post(
        "/api/v1/devices/import",
        files={"file": ("bad.csv", io.BytesIO(bad_csv), "text/csv")},
    )
    assert resp.status_code == 400


# ------------------------------------------------------------- gateways ----
def test_import_gateways(client):
    csv_bytes = (
        "网关编号,网关名称,ip地址,端口,协议\n"
        "GW-1,车间网关1,192.168.1.10,502,MQTT\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/v1/gateways/import",
        files={"file": ("gw.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 1
    gws = client.get("/api/v1/gateways").json()
    assert any(g["gateway_code"] == "GW-1" and g["port"] == 502 for g in gws)


# --------------------------------------------------------------- assets ----
def test_import_assets_links_device(client):
    device = _create_device(client, "Analyzer-ASSET1")
    csv_bytes = (
        f"资产编号,资产名称,类别,设备编号,采购金额\nAST-1,在线分析仪主机,分析设备,{device['device_code']},150000\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/v1/assets/import",
        files={"file": ("assets.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 1
    assets = client.get("/api/v1/assets").json()
    ast = next(a for a in assets if a["asset_code"] == "AST-1")
    assert ast["device_id"] == device["id"]


# ----------------------------------------------------------- maintenance ---
def test_import_work_orders(client):
    device = _create_device(client, "Analyzer-WO1")
    csv_bytes = (
        f"标题,设备编号,类型,优先级,状态\n更换采样探头,{device['device_code']},repair,high,open\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/v1/maintenance/work-orders/import",
        files={"file": ("wo.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inserted"] == 1
    wos = client.get("/api/v1/maintenance/work-orders").json()
    assert any(w["device_id"] == device["id"] and w["priority"] == "high" for w in wos)


# ------------------------------------------------------------------- lab ---
def test_import_lab_samples(client):
    device = _create_device(client, "Analyzer-LAB1")
    csv_bytes = (
        f"设备编号,组分,化验值,在线值\n{device['device_code']},甲烷,85.1,85.4\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/v1/lab/samples/import",
        files={"file": ("lab.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inserted"] == 1
    samples = client.get("/api/v1/lab/samples", params={"device_id": device["id"]}).json()
    assert len(samples) == 1
    assert samples[0]["component_name"] == "甲烷"


# -------------------------------------------------------------- knowledge --
def test_import_knowledge_documents_creates_searchable_chunks(client):
    csv_bytes = (
        "标题,内容\n"
        "色谱仪常见故障,色谱仪载气压力过低会导致基线漂移，需检查减压阀与管路密封。\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/v1/knowledge/documents/import",
        files={"file": ("kb.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inserted"] == 1

    docs = client.get("/api/v1/knowledge/documents").json()
    assert any(d["title"] == "色谱仪常见故障" for d in docs)

    search = client.get("/api/v1/knowledge/search", params={"q": "载气压力"}).json()
    assert len(search) >= 1


# -------------------------------------------------------------- analysis ---
def test_import_analysis_creates_dataset(client):
    device = _create_device(client, "Analyzer-AN1")
    csv_bytes = (
        b"Time,Nitrogen,Methane\n2026-09-18 00:00:00,40.1,85.2\n2026-09-18 01:00:00,40.5,85.0\n"
    )
    resp = client.post(
        "/api/v1/analysis/import",
        data={"device_id": device["id"]},
        files={"file": ("an.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["x_field"] is not None
    assert set(body["y_fields"]) == {"Nitrogen", "Methane"}

    datasets = client.get("/api/v1/analysis/datasets").json()
    assert any(d["id"] == body["id"] for d in datasets)


# --------------------------------------------------------- AI live binding -
def test_ai_chat_reads_live_device_data(client):
    device = _create_device(client, "Analyzer-CHAT1")
    resp = client.post(
        "/api/v1/ai/chat",
        json={"message": f"设备 {device['device_code']} 现在状态怎么样？"},
    )
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert device["device_code"] in answer


def test_ai_chat_overview_mentions_device_counts(client):
    _create_device(client, "Analyzer-CHAT2")
    resp = client.post("/api/v1/ai/chat", json={"message": "现在设备情况怎么样？"})
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert "设备总数" in answer


# --------------------------------------------------- import templates -----
_TEMPLATE_ENDPOINTS = [
    "/devices/import/template",
    "/devices/tags/import/template",
    "/gateways/import/template",
    "/assets/import/template",
    "/maintenance/work-orders/import/template",
    "/lab/samples/import/template",
    "/knowledge/documents/import/template",
    "/analysis/import/template",
]


def test_import_templates_download_with_chinese_filenames(client):
    """Regression test: the Content-Disposition header carries a Chinese
    filename (e.g. 设备导入模板.xlsx). Raw HTTP headers are latin-1 only, so
    passing that straight through used to raise an uncaught UnicodeEncodeError
    -> bare HTTP 500 for every single template endpoint. Must be RFC 6266
    filename*-encoded instead, and always return a real, non-trivial .xlsx."""
    import openpyxl

    for path in _TEMPLATE_ENDPOINTS:
        resp = client.get("/api/v1" + path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"
        assert resp.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        cd = resp.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert "filename*=UTF-8''" in cd
        # the ASCII fallback filename must be plain ASCII (no raw Chinese) —
        # that's the part latin-1 header encoding chokes on if not escaped
        fallback = cd.split("filename=", 1)[1].split(";", 1)[0].strip()
        fallback.encode("latin-1")  # raises if not latin-1-safe

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        header_row = [c.value for c in ws[1]]
        assert len(header_row) >= 2
        assert ws.max_row >= 2  # at least one example row beneath the header


def test_devices_template_round_trips_through_import(client):
    """Download the devices template, add a row, upload it back — the
    columns must be auto-detected and the row imported, end to end."""
    import openpyxl

    resp = client.get("/api/v1/devices/import/template")
    assert resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    ws = wb.active
    ws.append(["DEV-FROM-TEMPLATE", "来自模板的设备", "GC-2000", "Modbus TCP", "车间三", "", "online", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    up = client.post(
        "/api/v1/devices/import",
        files={"file": ("filled.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["inserted"] >= 1

    devices = client.get("/api/v1/devices").json()
    assert any(d["device_code"] == "DEV-FROM-TEMPLATE" for d in devices)


# --------------------------------------------- local-AI package installer -
def test_list_py_packages(client):
    resp = client.get("/api/v1/ai/packages")
    assert resp.status_code == 200
    pkgs = resp.json()
    ids = {p["id"] for p in pkgs}
    assert {"llama-cpu", "llama-cuda121", "numpy", "sklearn", "statsmodels"} <= ids
    for p in pkgs:
        assert "installed" in p
        assert "name" in p and "name_en" in p


def test_install_unknown_package_returns_400(client):
    resp = client.post("/api/v1/ai/packages/does-not-exist/install")
    assert resp.status_code == 400


def test_install_refuses_inside_frozen_exe(client, monkeypatch):
    from app.services import py_packages

    monkeypatch.setattr(py_packages, "is_frozen", lambda: True)
    resp = client.post("/api/v1/ai/packages/llama-cpu/install")
    assert resp.status_code == 400
    assert "build_exe.bat" in resp.json()["detail"] or "重新打包" in resp.json()["detail"]
