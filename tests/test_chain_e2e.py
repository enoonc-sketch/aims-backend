"""纵向打穿:

    设备 → TAG → 通讯 → 实时数据 → 数据质量 → 报警 → AI诊断 → 知识库 → 工单 → 维修结果 → AI反馈

这里不是逐个模块测, 而是让一条数据真的从网关推进来, 一路走到维修复盘、再看这次复盘如何影响下一次诊断。
"""
from datetime import datetime, timedelta, timezone

from app.models.device import DeviceModel
from app.models.maintenance import MaintenanceSOP

API = "/api/v1"


def _model_with_sop(db_session):
    model = DeviceModel(model_name="GC-9000", manufacturer="ACME", analyzer_type="GC")
    db_session.add(model)
    db_session.flush()
    db_session.add(
        MaintenanceSOP(
            device_model_id=model.id, sop_type="Calibration", title="GC-9000 标定规程",
            content="1) 通标气 2) 调零 3) 校准量程", version="1.0",
        )
    )
    db_session.add(
        MaintenanceSOP(
            device_model_id=model.id, sop_type="Maintenance", title="GC-9000 日常维护",
            content="检查气路与过滤器", version="1.0",
        )
    )
    db_session.flush()
    return model


def _setup_chain(client, db_session, code="E2E-1", gw_code="GW-E2E-1", address="40001", with_rule=True):
    """设备 + 网关 + TAG(绑网关和地址) + 阈值规则 + 一篇相关知识库文档。"""
    model = _model_with_sop(db_session)
    device = client.post(
        f"{API}/devices", json={"device_code": code, "name": f"{code} 分析仪", "model_id": model.id}
    ).json()
    gw = client.post(f"{API}/gateways", json={"gateway_code": gw_code, "name": gw_code}).json()
    tag = client.post(
        f"{API}/devices/{device['id']}/components",
        json={"component_name": "Methane", "unit": "%", "low_limit": 0, "high_limit": 100,
              "gateway_code": gw_code, "tag_address": address},
    )
    assert tag.status_code == 201, tag.text
    rule = None
    if with_rule:
        rule = client.post(
            f"{API}/alarms/rules",
            json={"device_id": device["id"], "component_name": "Methane", "high_limit": 30, "severity": "warning"},
        ).json()
    doc = client.post(f"{API}/knowledge/documents", json={"title": "Methane 漂移排查手册", "doc_type": "SOP"}).json()
    client.post(
        f"{API}/knowledge/chunks",
        json={"document_id": doc["id"], "chunk_index": 0,
              "content": "Methane analyzer drift high_reading: 先检查标气是否耗尽，再检查采样管线污染，最后检查检测器老化。"},
    )
    return device, gw, rule


def _push(client, gw_code, address, values, quality="good", start=None):
    start = start or datetime.now(timezone.utc) - timedelta(minutes=len(values))
    pts = [
        {"tag_address": address, "value": v, "time": (start + timedelta(minutes=i)).isoformat(), "quality": quality}
        for i, v in enumerate(values)
    ]
    r = client.post(f"{API}/communication/ingest", json={"gateway_code": gw_code, "points": pts})
    assert r.status_code == 200, r.text
    return r.json()


def _alarms(client, device_id, **params):
    return client.get(f"{API}/alarms", params={"device_id": device_id, **params}).json()


# ---------------------------------------------------------------------------
def test_full_chain_from_gateway_push_to_ai_feedback(client, db_session):
    device, gw, _rule = _setup_chain(client, db_session)

    # 通讯 -> 实时数据 -> 数据质量 -> 报警 -> AI诊断 -> 知识库 -> 工单: 一次推送走完
    normal = [20.0, 20.5, 21.0, 20.8, 21.2, 21.0, 20.9, 21.1, 21.0, 20.7]
    rising = [24.0, 27.0, 31.0, 34.0]
    res = _push(client, gw["gateway_code"], "40001", normal + rising)
    assert res["accepted"] == 14 and res["unmapped_addresses"] == []
    assert res["quality"]["bad"] == 0
    assert res["alarms_created"] == 1 and res["work_orders_created"] == 1

    # 通讯: 网关被置 online, 设备被置 online
    assert client.get(f"{API}/gateways/{gw['id']}").json()["status"] == "online"
    assert client.get(f"{API}/devices/{device['id']}").json()["status"] == "online"

    alarm = _alarms(client, device["id"])[0]
    assert alarm["alarm_type"] == "process" and alarm["value"] == 31.0

    # 报警 -> AI诊断: 诊断绑定报警, 依据里有证据统计
    diagnoses = client.get(f"{API}/ai/diagnoses", params={"alarm_id": alarm["id"]}).json()
    assert len(diagnoses) == 1
    diag = diagnoses[0]
    assert diag["source"] == "alarm_auto" and diag["alarm_id"] == alarm["id"]
    assert diag["evidence"]["points"] >= 10
    # 诊断依据(AI诊断 -> 知识库): 命中知识库文档 + 建议了 SOP
    assert any("Methane" in (h["document_title"] or "") for h in diag["evidence"]["knowledge"])
    assert diag["evidence"]["sop"]["title"] == "GC-9000 日常维护" or diag["evidence"]["sop"]["sop_type"] in ("Calibration", "Maintenance")

    # 知识库 -> 工单: 工单带着诊断、知识库摘录、SOP
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    assert wo["alarm_id"] == alarm["id"] and wo["diagnosis_id"] == diag["id"] and wo["sop_id"]
    assert "AI诊断" in wo["description"] and "知识库参考" in wo["description"] and "建议 SOP" in wo["description"]

    # 全链路视图: 走到了工单, 明确指出后面还没闭环
    chain = client.get(f"{API}/alarms/{alarm['id']}/chain").json()
    for stage in ("device", "tag", "communication", "realtime_data", "data_quality", "alarm",
                  "ai_diagnosis", "knowledge_base", "work_order"):
        assert chain["stages"][stage], stage
    assert not chain["stages"]["repair_result"] and not chain["closed_loop"]
    assert any("工单尚未完成" in b for b in chain["break_points"])

    # 工单 -> 维修结果 -> AI反馈
    done = client.post(
        f"{API}/maintenance/work-orders/{wo['id']}/complete",
        json={"action_taken": "更换标气并重新标定", "outcome": "fixed", "root_cause": "标气耗尽导致读数漂移",
              "parts_used": "标气 x1", "cost": 320.0, "diagnosis_verdict": "correct"},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["work_order"]["status"] == "done" and body["work_order"]["closed_at"]
    assert body["alarm_status"] == "resolved"
    assert body["record"]["outcome"] == "fixed"
    assert body["feedback_id"] and body["knowledge_document_id"]

    chain = client.get(f"{API}/alarms/{alarm['id']}/chain").json()
    assert chain["stages"]["repair_result"] and chain["stages"]["ai_feedback"]
    assert chain["ai_feedback"]["verdict"] == "correct"
    assert chain["closed_loop"], chain["break_points"]

    # 维修结果 -> 知识库: 案例写回, 下次同类检索能命中
    hits = client.get(f"{API}/knowledge/search", params={"q": "Methane 标气耗尽"}).json()
    assert any(h["document_title"].startswith("维修案例") for h in hits)

    # 二次完成同一张工单会被拒绝
    again = client.post(
        f"{API}/maintenance/work-orders/{wo['id']}/complete",
        json={"action_taken": "x", "outcome": "fixed"},
    )
    assert again.status_code == 409


def test_second_diagnosis_cites_the_repair_case_written_back_by_first(client, db_session):
    """维修结果 -> 知识库 -> (下一次) AI诊断: 闭环真的会"学"。"""
    device, gw, _ = _setup_chain(client, db_session, code="E2E-LOOP", gw_code="GW-LOOP", address="40010")
    _push(client, gw["gateway_code"], "40010", [20.0] * 5 + [21.0, 20.5, 20.8, 21.2, 20.9] + [33.0])
    alarm = _alarms(client, device["id"])[0]
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    client.post(
        f"{API}/maintenance/work-orders/{wo['id']}/complete",
        json={"action_taken": "更换采样管线滤芯", "outcome": "fixed",
              "root_cause": "采样管线滤芯堵塞", "diagnosis_verdict": "partially_correct"},
    )
    # 对一条新报警重跑诊断 —— 知识库里现在有了这次真实案例
    again = client.post(f"{API}/alarms/{alarm['id']}/diagnose")
    assert again.status_code == 200
    titles = [h["document_title"] for h in again.json()["evidence"]["knowledge"]]
    assert any(t.startswith("维修案例") for t in titles), titles


def test_bad_quality_readings_do_not_raise_process_alarms_but_raise_a_data_quality_alarm(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-BAD", gw_code="GW-BAD", address="40020")
    # 设备自己报 bad: 即使数值越限也不该产生工艺报警(这是坏数据, 不是工艺异常)
    res = _push(client, gw["gateway_code"], "40020", [99.0, 98.0, 97.0, 96.0], quality="bad")
    assert res["quality"]["bad"] == 4
    assert res["alarms_created"] == 0
    assert res["data_quality_alarms_created"] == 1

    alarms = _alarms(client, device["id"])
    assert [a["alarm_type"] for a in alarms] == ["data_quality"]
    # 这些读数在库里带着 bad 标记, 而不是被静默写成 good
    hist = client.get(f"{API}/measurements/history", params={"device_id": device["id"]}).json()
    assert {r["quality"] for r in hist["items"]} == {"bad"}
    # 数据质量报警同样走 诊断 -> 工单, 且工单类型是 inspection(去现场查, 不是直接修)
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    assert wo["order_type"] == "inspection" and wo["diagnosis_id"]


def test_uncertain_quality_still_alarms_and_says_so(client, db_session):
    """存疑 ≠ 坏: 统计判断只标 uncertain, 不压制报警(否则真实工艺突变会被当成"尖峰"漏报)。"""
    device, gw, _ = _setup_chain(client, db_session, code="E2E-UNC", gw_code="GW-UNC", address="40030")
    res = _push(client, gw["gateway_code"], "40030", [20.0] * 10 + [45.0], quality="uncertain")
    assert res["quality"]["uncertain"] >= 1 and res["alarms_created"] == 1
    assert "数据质量存疑" in _alarms(client, device["id"])[0]["message"]


def test_readings_after_a_spike_are_flagged_but_only_isolated_points(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-SPK", gw_code="GW-SPK", address="40040", with_rule=False)
    base = [20.0 + 0.1 * (i % 3) for i in range(15)]
    res = _push(client, gw["gateway_code"], "40040", base + [80.0, 20.1])
    assert res["quality"]["uncertain"] == 1  # 只有孤立的那一个尖峰点
    # 水平跃迁(连续多点都在新水平)不应被逐点标成尖峰
    res2 = _push(client, gw["gateway_code"], "40040", [60.0 + 0.1 * i for i in range(8)],
                 start=datetime.now(timezone.utc) + timedelta(seconds=30))
    assert res2["quality"]["uncertain"] <= 1


def test_acknowledged_alarm_does_not_spawn_a_duplicate_alarm_and_work_order(client, db_session, test_user):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-ACK", gw_code="GW-ACK", address="40050")
    _push(client, gw["gateway_code"], "40050", [31.0])
    alarm = _alarms(client, device["id"])[0]
    client.post(f"{API}/alarms/{alarm['id']}/acknowledge", json={"user_id": test_user.id})
    res = _push(client, gw["gateway_code"], "40050", [33.0, 35.0],
                start=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert res["alarms_created"] == 0 and res["work_orders_created"] == 0
    assert len(_alarms(client, device["id"])) == 1
    assert len(client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()) == 1


def test_unmapped_addresses_are_reported_not_silently_dropped(client, db_session):
    _device, gw, _ = _setup_chain(client, db_session, code="E2E-UNM", gw_code="GW-UNM", address="40060")
    r = client.post(
        f"{API}/communication/ingest",
        json={"gateway_code": gw["gateway_code"], "points": [
            {"tag_address": "40060", "value": 1.0}, {"tag_address": "49999", "value": 2.0}]},
    ).json()
    assert r["accepted"] == 1 and r["unmapped_addresses"] == ["49999"]
    assert client.post(f"{API}/communication/ingest",
                       json={"gateway_code": "NOPE", "points": [{"tag_address": "1", "value": 1}]}).status_code == 404


def test_gateway_retransmission_is_idempotent(client, db_session):
    _device, gw, _ = _setup_chain(client, db_session, code="E2E-DUP", gw_code="GW-DUP", address="40070", with_rule=False)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    first = _push(client, gw["gateway_code"], "40070", [1.0, 2.0, 3.0], start=t0)
    second = _push(client, gw["gateway_code"], "40070", [1.0, 2.0, 3.0], start=t0)
    assert first["accepted"] == 3 and second["accepted"] == 0 and second["duplicates_skipped"] == 3


def test_tag_address_must_be_unique_per_gateway(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-UNQ", gw_code="GW-UNQ", address="40080", with_rule=False)
    clash = client.post(
        f"{API}/devices/{device['id']}/components",
        json={"component_name": "Ethane", "gateway_code": gw["gateway_code"], "tag_address": "40080"},
    )
    assert clash.status_code == 409


def test_comm_loss_scan_raises_alarm_then_data_resumption_resolves_it(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-COMM", gw_code="GW-COMM", address="40090", with_rule=False)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    _push(client, gw["gateway_code"], "40090", [20.0, 20.1, 20.2], start=old)

    scan = client.post(f"{API}/communication/scan-stale", params={"stale_minutes": 30}).json()
    assert scan["comm_alarms_created"] == 1 and scan["stale_tags"][0]["component_name"] == "Methane"
    assert scan["gateways_marked_offline"] == ["GW-COMM"] and scan["devices_marked_offline"] == ["E2E-COMM"]
    assert client.get(f"{API}/gateways/{gw['id']}").json()["status"] == "offline"

    comm_alarm = [a for a in _alarms(client, device["id"]) if a["alarm_type"] == "comm"][0]
    diag = client.get(f"{API}/ai/diagnoses", params={"alarm_id": comm_alarm["id"]}).json()[0]
    assert "通讯" in diag["diagnosis"]
    # 重复扫描不重复开报警
    assert client.post(f"{API}/communication/scan-stale", params={"stale_minutes": 30}).json()["comm_alarms_created"] == 0

    # 数据恢复 -> 通讯中断报警自动关闭, 网关/设备回到 online
    _push(client, gw["gateway_code"], "40090", [20.3])
    comm_alarm = [a for a in _alarms(client, device["id"]) if a["alarm_type"] == "comm"][0]
    assert comm_alarm["status"] == "resolved"
    assert client.get(f"{API}/gateways/{gw['id']}").json()["status"] == "online"


def test_chain_view_points_at_the_broken_link(client, db_session):
    """TAG 没绑网关 -> 数据只能手工进 -> 全链路视图明确指出"TAG→通讯 断开"。"""
    model = _model_with_sop(db_session)
    device = client.post(f"{API}/devices", json={"device_code": "E2E-BRK", "name": "x", "model_id": model.id}).json()
    client.post(f"{API}/alarms/rules",
                json={"device_id": device["id"], "component_name": "Nitrogen", "high_limit": 10, "severity": "warning"})
    client.post(f"{API}/measurements", json={"device_id": device["id"], "component_name": "Nitrogen", "value": 50})
    alarm = _alarms(client, device["id"])[0]
    chain = client.get(f"{API}/alarms/{alarm['id']}/chain").json()
    assert chain["stages"]["tag"] and not chain["stages"]["communication"]
    assert any("TAG→通讯 断开" in b for b in chain["break_points"])
    assert not chain["closed_loop"]


def test_close_without_result_is_visible_as_pending_feedback(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-CLS", gw_code="GW-CLS", address="40100")
    _push(client, gw["gateway_code"], "40100", [35.0])
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    client.post(f"{API}/maintenance/work-orders/{wo['id']}/close")
    pending = client.get(f"{API}/ai/feedback/pending").json()
    assert any(p["work_order_id"] == wo["id"] for p in pending)
    alarm = _alarms(client, device["id"])[0]
    chain = client.get(f"{API}/alarms/{alarm['id']}/chain").json()
    assert any("close 直接关掉" in b for b in chain["break_points"]) and not chain["closed_loop"]

    # 事后补评价 -> 从待评价清单里消失
    diag_id = wo["diagnosis_id"]
    r = client.post(f"{API}/ai/diagnoses/{diag_id}/feedback", json={"verdict": "incorrect", "actual_root_cause": "误报"})
    assert r.status_code == 200 and r.json()["verdict"] == "incorrect"
    assert not any(p["work_order_id"] == wo["id"] for p in client.get(f"{API}/ai/feedback/pending").json())


def test_confidence_is_calibrated_by_rated_repair_history(client, db_session):
    """3 次同类诊断都被评为 incorrect 之后, 同类新诊断的置信度应明显低于规则包的固定值。"""
    device, gw, _ = _setup_chain(client, db_session, code="E2E-CAL", gw_code="GW-CAL", address="40110", with_rule=False)
    _push(client, gw["gateway_code"], "40110", [20.0 + 0.1 * (i % 3) for i in range(12)])

    def diagnose():
        r = client.post(f"{API}/ai/diagnose",
                        json={"device_id": device["id"], "component_name": "Methane", "symptom": "drift high"})
        assert r.status_code == 200, r.text
        return r.json()

    baseline = diagnose()
    for _ in range(3):
        d = diagnose()
        r = client.post(f"{API}/ai/diagnoses/{d['id']}/feedback", json={"verdict": "incorrect"})
        assert r.status_code == 200
    calibrated = diagnose()
    assert calibrated["confidence"] < baseline["confidence"] - 0.15

    stats = client.get(f"{API}/ai/feedback/stats", params={"component_name": "Methane", "symptom_family": "drift"}).json()
    assert stats["rated"] == 3 and stats["incorrect"] == 3 and stats["accuracy"] == 0.0


def test_partial_repair_keeps_work_order_open_and_alarm_unresolved(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-PRT", gw_code="GW-PRT", address="40120")
    _push(client, gw["gateway_code"], "40120", [40.0])
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    r = client.post(f"{API}/maintenance/work-orders/{wo['id']}/complete",
                    json={"action_taken": "紧固接头", "outcome": "not_fixed"}).json()
    assert r["work_order"]["status"] == "in_progress" and r["alarm_status"] == "active"
    assert r["knowledge_document_id"] is None  # 没修好的不写进知识库当"案例"
    # 之后修好了: 同一张工单、同一条反馈被更新, 而不是产生第二条
    r2 = client.post(f"{API}/maintenance/work-orders/{wo['id']}/complete",
                     json={"action_taken": "更换接头", "outcome": "fixed", "root_cause": "接头漏气",
                           "diagnosis_verdict": "correct"}).json()
    assert r2["work_order"]["status"] == "done" and r2["alarm_status"] == "resolved"
    assert r2["feedback_id"] == r["feedback_id"]


def test_manual_diagnose_bound_to_alarm_refreshes_open_work_order(client, db_session):
    device, gw, _ = _setup_chain(client, db_session, code="E2E-MAN", gw_code="GW-MAN", address="40130")
    _push(client, gw["gateway_code"], "40130", [20.0] * 6 + [36.0])
    alarm = _alarms(client, device["id"])[0]
    r = client.post(f"{API}/ai/diagnose",
                    json={"device_id": device["id"], "component_name": "Methane", "alarm_id": alarm["id"]})
    assert r.status_code == 200 and r.json()["alarm_id"] == alarm["id"]
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    assert wo["diagnosis_id"] == r.json()["id"]
    # alarm 不属于该设备 -> 422
    other = client.post(f"{API}/devices", json={"device_code": "E2E-OTH", "name": "o"}).json()
    bad = client.post(f"{API}/ai/diagnose",
                      json={"device_id": other["id"], "component_name": "Methane", "alarm_id": alarm["id"]})
    assert bad.status_code == 422


def test_file_import_marks_quality_and_uses_the_same_chain(client, db_session):
    import io

    device, _gw, _ = _setup_chain(client, db_session, code="E2E-IMP", gw_code="GW-IMP", address="40140")
    csv = "time,Methane\n" + "\n".join(
        f"{(datetime.now(timezone.utc) - timedelta(minutes=30 - i)).isoformat()},{20 + 0.1 * i}" for i in range(20)
    ) + f"\n{(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},55\n"
    r = client.post(f"{API}/measurements/import", data={"device_id": str(device["id"])},
                    files={"file": ("m.csv", io.BytesIO(csv.encode()), "text/csv")})
    assert r.status_code == 201, r.text
    alarms = _alarms(client, device["id"])
    assert len(alarms) == 1 and alarms[0]["value"] == 55.0
    # 导入路径的报警也带诊断和工单
    assert client.get(f"{API}/ai/diagnoses", params={"alarm_id": alarms[0]["id"]}).json()
    wo = client.get(f"{API}/maintenance/work-orders", params={"device_id": device["id"]}).json()[0]
    assert wo["diagnosis_id"]
