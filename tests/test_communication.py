import socket
import struct
import threading
import time


def _start_tcp_listener(port: int):
    ready = threading.Event()

    def _serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        conn.close()
        srv.close()

    threading.Thread(target=_serve, daemon=True).start()
    ready.wait(timeout=2)
    time.sleep(0.05)


def _start_modbus_stub(port: int):
    ready = threading.Event()

    def _serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        data = conn.recv(64)
        tid = data[0:2]
        unit_id = data[6:7]
        resp = tid + struct.pack(">HH", 0, 5) + unit_id + struct.pack(">BBH", 0x03, 2, 1234)
        conn.sendall(resp)
        conn.close()
        srv.close()

    threading.Thread(target=_serve, daemon=True).start()
    ready.wait(timeout=2)
    time.sleep(0.05)


def test_generic_communication_test_success(client):
    _start_tcp_listener(18901)
    resp = client.post(
        "/api/v1/communication/test",
        json={"host": "127.0.0.1", "port": 18901, "protocol": "MQTT", "timeout": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["latency_ms"] is not None


def test_generic_communication_test_refused(client):
    resp = client.post(
        "/api/v1/communication/test",
        json={"host": "127.0.0.1", "port": 1, "protocol": "MQTT", "timeout": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "拒绝" in body["message"] or "失败" in body["message"]


def test_modbus_probe_via_api(client):
    _start_modbus_stub(18902)
    resp = client.post(
        "/api/v1/communication/test",
        json={"host": "127.0.0.1", "port": 18902, "protocol": "Modbus TCP", "timeout": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_device_test_connection_updates_status(client):
    payload = {
        "device_code": "Analyzer-COMM-01",
        "name": "Comm Test Analyzer",
        "protocol": "Modbus TCP",
        "ip_address": "127.0.0.1",
        "status": "offline",
    }
    resp = client.post("/api/v1/devices", json=payload)
    assert resp.status_code == 201
    device_id = resp.json()["id"]

    # closed port -> device should be marked as fault
    resp = client.post(f"/api/v1/devices/{device_id}/test-connection", params={"port": 1, "timeout": 1})
    assert resp.status_code == 200
    assert resp.json()["success"] is False

    resp = client.get(f"/api/v1/devices/{device_id}")
    assert resp.json()["status"] == "fault"

    # now bring up a real Modbus stub on that port and re-test -> online
    # (the device's protocol is "Modbus TCP", so the real Modbus-frame
    # probe is used here, not a bare TCP handshake)
    _start_modbus_stub(18903)
    resp = client.post(f"/api/v1/devices/{device_id}/test-connection", params={"port": 18903, "timeout": 2})
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    resp = client.get(f"/api/v1/devices/{device_id}")
    assert resp.json()["status"] == "online"


def test_device_without_ip_returns_400(client):
    payload = {
        "device_code": "Analyzer-COMM-02",
        "name": "No IP Analyzer",
        "protocol": "Modbus TCP",
        "status": "offline",
    }
    resp = client.post("/api/v1/devices", json=payload)
    device_id = resp.json()["id"]

    resp = client.post(f"/api/v1/devices/{device_id}/test-connection")
    assert resp.status_code == 400


def test_ssrf_guard_blocks_unregistered_private_target(client, db_session, monkeypatch):
    """阶段 1.3（审计项 M-4）：未登记在任何网关/TAG 上的内网地址，
    /communication/test 必须拒绝（防止被当内网端口扫描 oracle）。"""
    from app.services import ssrf_guard
    monkeypatch.setattr(
        ssrf_guard, "resolve_host", lambda host: [__import__("ipaddress").ip_address("10.20.30.40")]
    )
    resp = client.post(
        "/api/v1/communication/test",
        json={"host": "internal.lab.example", "port": 502, "protocol": "Modbus TCP", "timeout": 2},
    )
    assert resp.status_code == 400
    assert "未登记" in resp.json()["detail"]


def test_ssrf_guard_allows_registered_gateway_target(client, db_session, monkeypatch):
    """已登记网关地址（即使是局域网 IP）→ 正当运维场景，放行。"""
    from app.models.device import Gateway
    from app.services import ssrf_guard
    db_session.add(Gateway(gateway_code="SSRF-GW-1", name="g", ip_address="192.168.10.5", port=502))
    db_session.commit()
    # resolve_host 无需真实 DNS：192.168.10.5 本身是 IP 字面量
    resp = client.post(
        "/api/v1/communication/test",
        json={"host": "192.168.10.5", "port": 502, "protocol": "Modbus TCP", "timeout": 1},
    )
    assert resp.status_code == 200  # 连不通也没关系——不是 400 拦截即可
