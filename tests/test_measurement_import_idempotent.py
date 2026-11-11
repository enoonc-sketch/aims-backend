"""测点导入的幂等性（阶段 9 性能测试暴露的真实缺陷）。

现场重复导入同一份 CSV 是常态（补录、修正、断线重发）。原来
`bulk_save_objects` 遇到主键 (time, device_id, component_name) 冲突直接抛
UniqueViolation，接口返回 HTTP 500 + 堆栈——数据其实只是"重复"。

修复后：幂等 upsert，重复导入成功、行数不翻倍、值取最新一次。
"""
from datetime import datetime

import pytest

from app.models.device import Device
from app.models.measurement import Measurement

CSV_HEADER = "timestamp,K-201,K-202\n"


def _csv(rows, value=61.0):
    lines = [CSV_HEADER.rstrip("\n")]
    for i in range(rows):
        ts = f"2026-03-01 00:{i:02d}:00"
        lines.append(f"{ts},{value + i},{value + i + 0.5}")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture()
def device(db_session):
    d = Device(device_code="IDEMP-DEV", name="幂等导入测试设备", protocol="Modbus TCP")
    db_session.add(d)
    db_session.flush()
    return d


def _import(client, device_id, payload):
    return client.post(
        "/api/v1/measurements/import",
        files={"file": ("idempot.csv", payload, "text/csv")},
        data={"device_id": str(device_id)},
    )


def test_reimport_same_file_is_idempotent(client, db_session, device):
    first = _import(client, device.id, _csv(5, 61.0))
    assert first.status_code in (200, 201), first.text

    second = _import(client, device.id, _csv(5, 70.0))
    assert second.status_code in (200, 201), f"重复导入不应 500：{second.status_code} {second.text}"

    db_session.expire_all()
    rows = db_session.query(Measurement).filter(Measurement.device_id == device.id).all()
    assert len(rows) == 10, f"重复导入不应新增行（期望 5 时间点 × 2 测点 = 10）：实际 {len(rows)}"
    # 值应更新为第二次导入的内容（"重导是为了修正"）
    values = sorted(r.value for r in rows if r.component_name == "K-201")
    assert values == [70.0, 71.0, 72.0, 73.0, 74.0], values


def test_partial_overlap_updates_only_existing_points(client, db_session, device):
    """时间窗部分重叠：重叠点更新、新点插入，总行数等于并集。"""
    assert _import(client, device.id, _csv(3, 50.0)).status_code in (200, 201)
    # 时间点 02~04（02、03 与上批重叠，04 是新的）
    lines = [CSV_HEADER.rstrip("\n"),
             "2026-03-01 00:02:00,90.0,90.5",
             "2026-03-01 00:03:00,91.0,91.5",
             "2026-03-01 00:04:00,92.0,92.5"]
    payload = ("\n".join(lines) + "\n").encode()
    assert _import(client, device.id, payload).status_code in (200, 201)

    db_session.expire_all()
    rows = db_session.query(Measurement).filter(Measurement.device_id == device.id).all()
    # 第一批 00:00~00:02，第二批 00:02~00:04 → 并集 5 个时间点 × 2 测点 = 10 行
    assert len(rows) == 10, f"重叠时间点不应重复插入，期望 10 行，实际 {len(rows)}"
    at_02 = [r.value for r in rows if r.component_name == "K-202"
             and r.time.replace(tzinfo=None) == datetime(2026, 3, 1, 0, 2)]
    assert at_02 and at_02[0] == 90.5, at_02
