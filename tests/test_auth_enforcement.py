"""Auth end-to-end + enforcement tests.

Before this change, `auth.py` and `system.py` had *zero* test coverage, and
every other router had no auth at all — so nothing here was actually being
verified. These tests cover both directions:

1. The real login/me flow works with a real JWT (no dependency override).
2. A representative sample of write endpoints across the previously-open
   routers reject unauthenticated requests with 401, and accept them once
   logged in — proving the JWT framework is no longer decorative.
3. Read (GET) endpoints are intentionally left open (unauthenticated), which
   is the documented scope of this pass ("at least write operations").
4. get_current_superuser actually blocks non-superusers (audit logs).

Uses `anon_client` (real auth enforcement, no override) throughout, logging
in for real via /auth/login where a request needs to succeed.
"""
from app.core.security import hash_password
from app.models.system import User


def _make_user(db_session, *, username="alice", password="s3cret-pw", is_superuser=False, is_active=True):
    user = User(
        username=username,
        hashed_password=hash_password(password),
        full_name="Test User",
        is_active=is_active,
        is_superuser=is_superuser,
    )
    db_session.add(user)
    db_session.flush()
    return user, password


def _login(anon_client, username, password):
    resp = anon_client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def test_login_and_me_round_trip(anon_client, db_session):
    _make_user(db_session, username="bob", password="hunter2000")
    db_session.commit()

    resp = anon_client.post("/api/v1/auth/login", json={"username": "bob", "password": "wrong"})
    assert resp.status_code == 401

    token = _login(anon_client, "bob", "hunter2000")
    me = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "bob"

    # No token at all -> 401, not a silent "anonymous" pass-through.
    resp = anon_client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_disabled_user_cannot_login(anon_client, db_session):
    _make_user(db_session, username="disabled-user", password="whatever123", is_active=False)
    db_session.commit()
    resp = anon_client.post("/api/v1/auth/login", json={"username": "disabled-user", "password": "whatever123"})
    assert resp.status_code == 403


def test_communication_test_requires_auth(anon_client):
    """The generic host/port probe must not be callable by anyone who can
    merely reach the API — that's exactly the network-probe-as-a-service
    concern this endpoint was flagged for."""
    resp = anon_client.post(
        "/api/v1/communication/test",
        json={"host": "127.0.0.1", "port": 1, "protocol": "MQTT", "timeout": 1},
    )
    assert resp.status_code == 401


def test_write_endpoints_reject_anonymous_across_routers(anon_client, db_session):
    """Sample one write endpoint from each business router that used to have
    no auth at all, and confirm it now rejects an anonymous caller."""
    cases = [
        ("post", "/api/v1/devices", {"device_code": "AUTH-DEV-1", "name": "x", "protocol": "Modbus TCP"}),
        ("post", "/api/v1/gateways", {"gateway_code": "AUTH-GW-1", "name": "x"}),
        ("post", "/api/v1/alarms", {"device_id": 1, "message": "x", "severity": "warning"}),
        ("post", "/api/v1/assets", {"asset_code": "AUTH-AST-1", "name": "x"}),
        ("post", "/api/v1/lab/samples", {"device_id": 1, "component_name": "x"}),
        ("post", "/api/v1/knowledge/documents", {"title": "x", "content": "x"}),
        ("post", "/api/v1/maintenance/work-orders", {"title": "x", "device_id": 1}),
        ("post", "/api/v1/measurements", {"device_id": 1, "component_name": "x", "value": 1.0}),
    ]
    for method, path, payload in cases:
        resp = getattr(anon_client, method)(path, json=payload)
        assert resp.status_code == 401, f"{method.upper()} {path} should require auth, got {resp.status_code}: {resp.text}"


def test_write_endpoint_succeeds_once_authenticated(anon_client, db_session):
    _make_user(db_session, username="writer", password="writer-pw-123")
    db_session.commit()
    token = _login(anon_client, "writer", "writer-pw-123")

    resp = anon_client.post(
        "/api/v1/devices",
        json={"device_code": "AUTH-DEV-OK", "name": "x", "protocol": "Modbus TCP"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text


def test_read_endpoints_remain_open(anon_client):
    """Documented scope of this pass: reads stay open, only writes require auth."""
    for path in ("/api/v1/devices", "/api/v1/alarms", "/api/v1/assets", "/api/v1/gateways"):
        resp = anon_client.get(path)
        assert resp.status_code == 200, f"GET {path} should stay open, got {resp.status_code}"


def test_audit_logs_require_superuser(anon_client, db_session):
    _make_user(db_session, username="regular-joe", password="joe-pw-12345", is_superuser=False)
    db_session.commit()
    token = _login(anon_client, "regular-joe", "joe-pw-12345")

    resp = anon_client.get("/api/v1/system/audit-logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403

    # Anonymous doesn't even get that far -> 401 before the superuser check.
    resp = anon_client.get("/api/v1/system/audit-logs")
    assert resp.status_code == 401
