"""External data source module: CRUD (no password leak), import, continuity
gaps -> alarm -> work order, coverage/health metrics, lab-sample linkage."""
from datetime import datetime, timedelta, timezone
import ipaddress

from app.models.alarm import Alarm
from app.models.device import Device
from app.models.external_source import ExternalDatabase, ExternalSource, ExternalSyncLog
from app.models.maintenance import LabSample, WorkOrder
from app.models.measurement import Measurement
from app.services import data_continuity, data_health, external_import, ssrf_guard
from app.services.crypto_util import decrypt_secret, encrypt_secret


def _mock_dns(monkeypatch):
    """SSRF 校验（阶段 1.3）会解析抓取源 URL 的主机名；测试不依赖真实 DNS，
    统一 mock 成一个公网 IP。"""
    monkeypatch.setattr(
        ssrf_guard, "resolve_host", lambda host: [ipaddress.ip_address("93.184.216.34")]
    )


def _setup(db, interval=10):
    dev = Device(device_code="EXT-D1", name="ext dev")
    src = ExternalSource(
        name="s", base_url="https://x", login_url="https://x/login",
        username="u", password_encrypted=encrypt_secret("pw"),
    )
    db.add_all([dev, src])
    db.flush()
    edb = ExternalDatabase(
        source_id=src.id, name="db1", list_url="https://x/d1", expected_interval_minutes=interval
    )
    db.add(edb)
    db.flush()
    return dev, edb


def _csv(tmp_path, rows):
    p = tmp_path / "d.csv"
    p.write_text("device_code,component_name,time,value\n" + "\n".join(rows), encoding="utf-8")
    return p


def _series(start, n, step=10, base=20):
    return [f"EXT-D1,O2,{(start + timedelta(minutes=step * i)).isoformat()},{base + i}" for i in range(n)]


def test_crypto_roundtrip():
    assert decrypt_secret(encrypt_secret("p@ss")) == "p@ss"


def test_source_api_never_returns_password(client, monkeypatch):
    _mock_dns(monkeypatch)
    r = client.post("/api/v1/external/sources", json={
        "name": "s", "base_url": "https://vendor.example.com", "login_url": "https://vendor.example.com/login",
        "username": "u", "password": "topsecret",
    })
    assert r.status_code == 201
    assert "password_encrypted" not in r.text and "topsecret" not in r.text
    sid = r.json()["id"]
    r = client.post("/api/v1/external/databases", json={
        "source_id": sid, "name": "d", "list_url": "https://vendor.example.com/d"})
    assert r.status_code == 201
    assert client.get(f"/api/v1/external/databases/{r.json()['id']}/health").status_code == 200


def test_ssrf_guard_rejects_private_and_unresolvable_urls(client):
    """阶段 1.3（审计项 M-3）：抓取源 URL 必须是公网 http/https 地址。"""
    for payload, why in (
        ({"name": "s1", "base_url": "https://127.0.0.1", "login_url": "https://127.0.0.1/login",
          "username": "u", "password": "p"}, "loopback"),
        ({"name": "s2", "base_url": "file:///C:/Windows/win.ini", "login_url": "https://x.invalid",
          "username": "u", "password": "p"}, "file scheme"),
        ({"name": "s3", "base_url": "https://192.168.1.10", "login_url": "https://192.168.1.10/login",
          "username": "u", "password": "p"}, "RFC1918"),
    ):
        r = client.post("/api/v1/external/sources", json=payload)
        assert r.status_code == 400, f"{why} should be rejected, got {r.status_code}"


def test_import_dedup_and_unknown_device(db_session, tmp_path):
    _, edb = _setup(db_session)
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    rows = _series(start, 3) + ["NOPE,O2,2026-09-20T00:00:00+00:00,1"]
    p = _csv(tmp_path, rows)
    s1 = external_import.import_database_file(db_session, edb, p)
    s2 = external_import.import_database_file(db_session, edb, p)  # re-download same file
    assert s1.rows_imported == 3 and s2.rows_imported == 0
    assert any("不存在" in w for w in s1.warnings)


def test_gap_creates_alarm_and_work_order(db_session, tmp_path):
    dev, edb = _setup(db_session, interval=10)
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    rows = _series(start, 5) + _series(start + timedelta(hours=3), 3)  # ~2h20m hole
    stats = external_import.import_database_file(db_session, edb, _csv(tmp_path, rows))
    found = data_continuity.check_continuity_for_database(db_session, edb, stats.device_ids, lookback_hours=24 * 3650)
    assert found == 1
    # rerun is idempotent
    assert data_continuity.check_continuity_for_database(db_session, edb, stats.device_ids, lookback_hours=24 * 3650) == 0
    assert db_session.query(Alarm).filter_by(device_id=dev.id).count() == 1
    assert db_session.query(WorkOrder).filter_by(device_id=dev.id).count() == 1


def test_coverage_and_lab_linkage(db_session):
    dev, edb = _setup(db_session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # data for the first hour of a 3h window, then nothing for 2h+
    for i in range(6):
        db_session.add(Measurement(time=now - timedelta(minutes=170) + timedelta(minutes=10 * i),
                                   device_id=dev.id, component_name="O2", value=1.0))
    db_session.add_all([
        LabSample(device_id=dev.id, component_name="O2", sample_no="A",
                  sampled_at=now - timedelta(minutes=150), lab_value=1),
        LabSample(device_id=dev.id, component_name="O2", sample_no="B",
                  sampled_at=now - timedelta(hours=1), lab_value=1),
    ])
    db_session.flush()

    cov = data_health.coverage_summary(db_session, dev.id, "O2", expected_interval_minutes=10, hours=3)
    assert cov["actual_points"] == 6 and cov["coverage_pct"] < 50
    assert cov["currently_stale"] is True and cov["longest_gap_minutes"] > 100

    lab = data_health.lab_sample_coverage(db_session, dev.id, "O2", window_minutes=15)
    assert lab["sample_count"] == 2 and lab["covered_count"] == 1
    assert [i["covered"] for i in lab["items"]] == [True, False]


def test_database_health_counts_consecutive_failures(db_session):
    _, edb = _setup(db_session)
    for st in ("success", "failed", "failed"):
        db_session.add(ExternalSyncLog(database_id=edb.id, status=st, finished_at=datetime.now(timezone.utc)))
        db_session.flush()
    h = data_health.database_health(db_session, edb)
    assert h["consecutive_failures"] == 2 and h["success_rate_pct"] == 33.3


def test_refresh_without_browser_fails_cleanly_and_is_logged(client, monkeypatch):
    """Manual refresh must never 500: a missing/broken browser or wrong
    selectors land in the sync log + database.last_error for the UI."""
    _mock_dns(monkeypatch)
    sid = client.post("/api/v1/external/sources", json={
        "name": "s", "base_url": "https://vendor.example.com", "login_url": "https://vendor.example.com/login",
        "username": "u", "password": "p"}).json()["id"]
    did = client.post("/api/v1/external/databases", json={
        "source_id": sid, "name": "d", "list_url": "https://vendor.example.com/d"}).json()["id"]
    r = client.post(f"/api/v1/external/databases/{did}/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and body["error_message"]
    db = client.get(f"/api/v1/external/databases/{did}").json()
    assert db["last_status"] == "failed" and db["last_error"]
    logs = client.get(f"/api/v1/external/databases/{did}/logs").json()
    assert len(logs) == 1 and logs[0]["status"] == "failed"
    health = client.get(f"/api/v1/external/databases/{did}/health").json()
    assert health["consecutive_failures"] == 1
    assert client.get(f"/api/v1/external/sources/{sid}").json()["status"] in ("error", "login_failed")


def test_frontend_serves_data_management_page(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert 'id="page-ext"' in r.text and "showPage('ext'" in r.text


class _FakeSession:
    def __init__(self, source):
        pass

    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch_scraper(monkeypatch, csv_path):
    from app.services import external_sync_service as svc

    monkeypatch.setattr(svc, "external_session", _FakeSession)
    monkeypatch.setattr(svc, "download_from_database", lambda page, database, d: csv_path)
    return svc


def test_orchestration_success_path(db_session, tmp_path, monkeypatch):
    dev, edb = _setup(db_session, interval=10)
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    p = _csv(tmp_path, _series(start, 4) + _series(start + timedelta(hours=3), 2))
    svc = _patch_scraper(monkeypatch, p)
    log = svc.run_sync_for_database(db_session, edb, triggered_by="manual")
    assert log.status == "success" and log.rows_imported == 6
    assert edb.last_status == "success" and edb.last_rows_imported == 6
    assert db_session.query(Measurement).count() == 6


def test_midway_failure_rolls_back_partial_import(db_session, tmp_path, monkeypatch):
    dev, edb = _setup(db_session, interval=10)
    p = _csv(tmp_path, _series(datetime(2026, 9, 20, tzinfo=timezone.utc), 5))
    svc = _patch_scraper(monkeypatch, p)

    def boom(*a, **k):
        raise RuntimeError("continuity exploded")

    monkeypatch.setattr(svc.data_continuity, "check_continuity_for_database", boom)
    log = svc.run_sync_for_database(db_session, edb, triggered_by="scheduled")
    assert log.status == "failed" and "exploded" in log.error_message
    assert db_session.query(Measurement).count() == 0  # partial import discarded
    assert edb.last_status == "failed"
    # the device/source/database rows created before the sync survive
    assert db_session.get(Device, dev.id) is not None
