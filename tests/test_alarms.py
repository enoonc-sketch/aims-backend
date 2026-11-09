def _create_device(client, code="Analyzer-AL1"):
    return client.post("/api/v1/devices", json={"device_code": code, "name": code}).json()


def _create_user(db_session):
    from app.models.system import User

    user = User(username="tester", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_create_and_list_alarm(client):
    device = _create_device(client)
    resp = client.post(
        "/api/v1/alarms",
        json={
            "device_id": device["id"],
            "component_name": "Methane",
            "severity": "critical",
            "message": "High methane concentration",
            "value": 35.2,
        },
    )
    assert resp.status_code == 201
    alarm = resp.json()
    assert alarm["status"] == "active"

    resp = client.get("/api/v1/alarms", params={"device_id": device["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_acknowledge_and_resolve_alarm(client, db_session):
    device = _create_device(client, "Analyzer-AL2")
    user = _create_user(db_session)
    alarm = client.post(
        "/api/v1/alarms",
        json={"device_id": device["id"], "severity": "warning", "message": "Drift detected"},
    ).json()

    resp = client.post(f"/api/v1/alarms/{alarm['id']}/acknowledge", json={"user_id": user.id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "acknowledged"

    resp = client.post(f"/api/v1/alarms/{alarm['id']}/resolve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


def test_alarm_rule_crud(client):
    device = _create_device(client, "Analyzer-AL3")
    resp = client.post(
        "/api/v1/alarms/rules",
        json={
            "device_id": device["id"],
            "component_name": "Methane",
            "low_limit": 0,
            "high_limit": 30,
            "severity": "warning",
        },
    )
    assert resp.status_code == 201
    resp = client.get("/api/v1/alarms/rules", params={"device_id": device["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 1
