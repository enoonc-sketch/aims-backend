"""数据保留清理与孤儿批次回收（AIMS 1.0 阶段 1，审计项 D-1 / D-2）。"""
from datetime import datetime, timedelta, timezone

from app.models.analysis import DashboardSnapshot
from app.models.device import Device
from app.models.measurement import Measurement, MeasurementImport
from app.models.system import AuditLog, SystemSetting
from app.services.retention import get_retention_days, recover_orphan_imports, run_retention


def _mk_device(db_session):
    dev = Device(device_code="RET-DEV-1", name="retention dev", protocol="Modbus TCP")
    db_session.add(dev)
    db_session.flush()
    return dev


def test_default_retention_days(db_session):
    assert get_retention_days(db_session, "data_retention_days") == 365
    assert get_retention_days(db_session, "login_failure_retention_days") == 30


def test_retention_days_read_from_settings(db_session):
    db_session.add(SystemSetting(key="data_retention_days", value="30"))
    db_session.commit()
    assert get_retention_days(db_session, "data_retention_days") == 30
    # 非法值回退默认
    db_session.add(SystemSetting(key="audit_log_retention_days", value="abc"))
    db_session.commit()
    assert get_retention_days(db_session, "audit_log_retention_days") == 730


def test_run_retention_purges_old_rows(db_session):
    dev = _mk_device(db_session)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    recent = now - timedelta(days=10)
    db_session.add_all([
        Measurement(time=old, device_id=dev.id, component_name="CH4", value=1.0),
        Measurement(time=recent, device_id=dev.id, component_name="CH4", value=2.0),
        # 审计日志默认保留 730 天，用 800 天前的记录验证清理
        AuditLog(action="TEST", entity="t", created_at=now - timedelta(days=800)),
        # 看板快照默认保留 180 天
        DashboardSnapshot(total_devices=1, snapshot_at=old),
    ])
    db_session.commit()

    # 种子里的 data_retention_days=365 → 400 天前的读数应被清掉，10 天的保留
    out = run_retention(db_session, now=now)
    assert out["data_retention_days"] == 1
    remaining = db_session.query(Measurement).all()
    assert len(remaining) == 1 and remaining[0].value == 2.0
    assert out["audit_log_retention_days"] == 1
    assert out["snapshot_retention_days"] == 1


def test_retention_keeps_data_within_window(db_session):
    dev = _mk_device(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Measurement(
        time=now - timedelta(days=100), device_id=dev.id, component_name="CH4", value=1.0
    ))
    db_session.commit()
    out = run_retention(db_session, now=now)
    assert out["data_retention_days"] == 0
    assert db_session.query(Measurement).count() == 1


def test_recover_orphan_imports(db_session):
    dev = _mk_device(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        # 停留 processing 超过 60 分钟 → 应回收
        MeasurementImport(file_name="orphan.csv", device_id=dev.id, status="processing",
                          created_at=now - timedelta(hours=2)),
        # 刚开始的批次 → 不动
        MeasurementImport(file_name="fresh.csv", device_id=dev.id, status="processing",
                          created_at=now - timedelta(minutes=5)),
        # 已完成的批次 → 不动
        MeasurementImport(file_name="done.csv", device_id=dev.id, status="done",
                          created_at=now - timedelta(hours=3)),
    ])
    db_session.commit()
    n = recover_orphan_imports(db_session)
    assert n == 1
    statuses = {r.file_name: r.status for r in db_session.query(MeasurementImport).all()}
    assert statuses["orphan.csv"] == "failed"
    assert statuses["fresh.csv"] == "processing"
    assert statuses["done.csv"] == "done"
