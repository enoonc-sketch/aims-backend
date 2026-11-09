"""Regression tests for the 0.5.2 performance pass (N+1, GROUP BY stats, /trend cap)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.models.measurement import Measurement


def _mk_device(client, code, status="online", components=()):
    d = client.post("/api/v1/devices", json={"device_code": code, "name": code, "status": status}).json()
    for c in components:
        r = client.post(f"/api/v1/devices/{d['id']}/components", json={"component_name": c})
        assert r.status_code in (200, 201), r.text
    return d


def _count_selects(conn, fn):
    stmts = []

    def _before(_c, _cur, statement, *_a):
        if statement.lstrip().upper().startswith("SELECT"):
            stmts.append(statement)

    event.listen(conn, "before_cursor_execute", _before)
    try:
        fn()
    finally:
        event.remove(conn, "before_cursor_execute", _before)
    return stmts


def test_device_list_does_not_n_plus_one(client, db_session):
    for i in range(8):
        _mk_device(client, f"PERF-N1-{i}", components=("N2", "CH4"))
    db_session.expire_all()  # force relationships to be re-fetched, not served from the identity map

    out = {}
    stmts = _count_selects(
        db_session.get_bind(),
        lambda: out.update(r=client.get("/api/v1/devices", params={"limit": 100})),
    )
    resp = out["r"]
    assert resp.status_code == 200
    devs = [d for d in resp.json() if d["device_code"].startswith("PERF-N1-")]
    assert len(devs) == 8 and all(len(d["components"]) == 2 for d in devs)
    # 1 query for devices + 1 selectin for components (+ at most the auth/user lookup)
    assert len(stmts) <= 3, stmts


def test_dashboard_stats_counts_by_status(client):
    before = client.get("/api/v1/dashboard/stats").json()
    for i, st in enumerate(["online", "online", "warning", "fault", "offline"]):
        _mk_device(client, f"PERF-ST-{i}", status=st)
    after = client.get("/api/v1/dashboard/stats").json()
    assert after["total_devices"] - before["total_devices"] == 5
    assert after["online_devices"] - before["online_devices"] == 2
    assert after["warning_devices"] - before["warning_devices"] == 1
    assert after["fault_devices"] - before["fault_devices"] == 1


def test_trend_caps_points_and_keeps_newest_ascending(client, db_session):
    d = _mk_device(client, "PERF-TREND")
    now = datetime.now(timezone.utc)
    for i in range(40):  # i=0 oldest ... i=39 newest
        db_session.add(Measurement(time=now - timedelta(minutes=40 - i), device_id=d["id"],
                                   component_name="N2", value=float(i)))
    db_session.flush()

    r = client.get("/api/v1/measurements/trend",
                   params={"device_id": d["id"], "components": ["N2"], "hours": 24, "limit": 10})
    assert r.status_code == 200
    pts = r.json()[0]["points"]
    assert [p["value"] for p in pts] == [float(i) for i in range(30, 40)]  # newest 10, ascending

    full = client.get("/api/v1/measurements/trend",
                      params={"device_id": d["id"], "components": ["N2"], "hours": 24}).json()
    assert len(full[0]["points"]) == 40  # default cap (5000) leaves small series untouched

    assert client.get("/api/v1/measurements/trend",
                      params={"device_id": d["id"], "components": ["N2"], "limit": 0}).status_code == 422
