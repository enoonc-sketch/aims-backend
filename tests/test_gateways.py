import socket
import struct
import threading
import time


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


def _create_gateway(client, **overrides):
    payload = {
        "gateway_code": "GW-TEST-01",
        "name": "Test Gateway",
        "ip_address": "127.0.0.1",
        "port": 1,
        "protocol": "MQTT",
    }
    payload.update(overrides)
    resp = client.post("/api/v1/gateways", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_gateway_crud(client):
    created = _create_gateway(client)
    gid = created["id"]
    assert created["status"] == "offline"

    resp = client.get("/api/v1/gateways")
    assert resp.status_code == 200
    assert any(g["id"] == gid for g in resp.json())

    resp = client.get(f"/api/v1/gateways/{gid}")
    assert resp.status_code == 200
    assert resp.json()["gateway_code"] == "GW-TEST-01"

    resp = client.patch(f"/api/v1/gateways/{gid}", json={"port": 9999})
    assert resp.status_code == 200
    assert resp.json()["port"] == 9999

    resp = client.delete(f"/api/v1/gateways/{gid}")
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/gateways/{gid}")
    assert resp.status_code == 404


def test_gateway_duplicate_code_rejected(client):
    _create_gateway(client, gateway_code="GW-DUP")
    resp = client.post(
        "/api/v1/gateways",
        json={"gateway_code": "GW-DUP", "name": "Another", "ip_address": "1.1.1.1"},
    )
    assert resp.status_code == 409


def test_gateway_test_connection_updates_status_and_heartbeat(client):
    gateway = _create_gateway(client, protocol="Modbus TCP", port=1)
    gid = gateway["id"]

    # closed port -> fault, no heartbeat
    resp = client.post(f"/api/v1/gateways/{gid}/test-connection", params={"timeout": 1})
    assert resp.status_code == 200
    assert resp.json()["success"] is False

    resp = client.get(f"/api/v1/gateways/{gid}")
    body = resp.json()
    assert body["status"] == "fault"
    assert body["last_heartbeat"] is None

    # bring up a real Modbus stub and re-test -> online + heartbeat set
    _start_modbus_stub(18910)
    client.patch(f"/api/v1/gateways/{gid}", json={"port": 18910})
    resp = client.post(f"/api/v1/gateways/{gid}/test-connection", params={"timeout": 2})
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    resp = client.get(f"/api/v1/gateways/{gid}")
    body = resp.json()
    assert body["status"] == "online"
    assert body["last_heartbeat"] is not None


def test_gateway_test_connection_without_ip_returns_400(client):
    resp = client.post(
        "/api/v1/gateways",
        json={"gateway_code": "GW-NOIP", "name": "No IP Gateway"},
    )
    gid = resp.json()["id"]
    resp = client.post(f"/api/v1/gateways/{gid}/test-connection")
    assert resp.status_code == 400


def test_gateway_not_found(client):
    resp = client.get("/api/v1/gateways/999999")
    assert resp.status_code == 404
    resp = client.patch("/api/v1/gateways/999999", json={"name": "x"})
    assert resp.status_code == 404
    resp = client.delete("/api/v1/gateways/999999")
    assert resp.status_code == 404
