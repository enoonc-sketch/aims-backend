"""Template Center — 模板中心端到端测试（计划书第十二章验收项）。

覆盖：内置模板种子、创建/编辑/复制/删除、.aimstpl 打包与解析、
导入安全检查（路径穿越/zip 炸弹/坏 manifest/版本兼容）、导入预览+确认、
同 UID 冲突追加版本、应用模板生成真实业务数据、版本回退与比较、
导出往返（export → import round-trip）、系统模板保护。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.deploy import APP_VERSION
from app.services.template_package import (
    TemplatePackageError,
    build_package,
    parse_package,
)

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture()
def api(client: TestClient):
    """conftest 的 client 夹具已经把 get_current_user 覆盖为测试超级用户：
    所有请求天然"已登录"，无需真实 JWT。"""
    return _Api(client, {})


class _Api:
    def __init__(self, client: TestClient, headers: dict):
        self.client = client
        self.headers = headers

    def get(self, path, **kw):
        return self.client.get("/api/v1" + path, headers=self.headers, **kw)

    def post(self, path, **kw):
        return self.client.post("/api/v1" + path, headers=self.headers, **kw)

    def put(self, path, **kw):
        return self.client.put("/api/v1" + path, headers=self.headers, **kw)

    def delete(self, path, **kw):
        return self.client.delete("/api/v1" + path, headers=self.headers, **kw)


# --------------------------------------------------------------------------- #
# 内置模板（12.2-A）
# --------------------------------------------------------------------------- #
def test_builtin_templates_seeded_and_protected(api: _Api):
    rows = api.get("/templates", params={"source": "system"}).json()
    assert len(rows) >= 15, "内置官方模板应不少于 15 个"
    cats = {r["category"] for r in rows}
    for need in ("仪表管理", "气相色谱仪", "设备台账", "报警配置", "报表", "AI 分析", "RAG 知识库"):
        assert need in cats, f"缺少分类 {need}"
    sys_id = rows[0]["id"]
    # 系统模板禁止删除 / 改名
    assert api.delete(f"/templates/{sys_id}").status_code == 403
    assert api.put(f"/templates/{sys_id}", json={"name": "改名尝试"}).status_code == 403


def test_categories_endpoint(api: _Api):
    data = api.get("/templates/categories").json()
    assert len(data["categories"]) == 20


# --------------------------------------------------------------------------- #
# 创建 / 编辑 / 版本（12.2-B / 12.7）
# --------------------------------------------------------------------------- #
def _make_tpl(api: _Api, name="我的报警模板"):
    resp = api.post("/templates", json={
        "name": name, "category": "报警配置",
        "description": "测试模板", "author": "tester",
        "content": {"alarm_rules": [{"rule_type": "threshold", "severity": "warning",
                                     "match_component": "*", "high_limit_delta": 5}]},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_update_restore_and_diff(api: _Api):
    tpl = _make_tpl(api, name="版本管理测试模板")
    tid = tpl["id"]
    # 编辑 → 追加 v2
    resp = api.put(f"/templates/{tid}", json={
        "content": {"alarm_rules": [{"rule_type": "threshold", "severity": "critical",
                                     "match_component": "*", "high_limit_delta": 50}]},
        "changelog": "阈值调高",
    })
    assert resp.status_code == 200 and resp.json()["current_version"] == 2
    versions = api.get(f"/templates/{tid}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    # 版本比较
    diff = api.get(f"/templates/{tid}/diff", params={"from": 1, "to": 2}).json()
    assert diff["changes"], "v1→v2 应有差异"
    # 回退到 v1 → 生成 v3（历史不丢）
    resp = api.post(f"/templates/{tid}/versions/1/restore")
    assert resp.status_code == 200 and resp.json()["current_version"] == 3
    versions = api.get(f"/templates/{tid}/versions").json()
    assert [v["version"] for v in versions] == [1, 2, 3]
    # 不存在的版本
    assert api.get(f"/templates/{tid}/diff", params={"from": 1, "to": 99}).status_code == 404
    # 清理
    assert api.delete(f"/templates/{tid}").status_code == 204


def test_duplicate_and_favorite(api: _Api):
    tpl = _make_tpl(api, name="复制测试模板")
    tid = tpl["id"]
    copy = api.post(f"/templates/{tid}/duplicate").json()
    assert copy["name"].endswith("（副本）") and copy["source"] == "custom"
    fav = api.post(f"/templates/{tid}/favorite").json()
    assert fav["is_favorite"] is True
    rows = api.get("/templates", params={"favorite": "true"}).json()
    assert any(r["id"] == tid for r in rows)
    api.delete(f"/templates/{tid}")
    api.delete(f"/templates/{copy['id']}")


# --------------------------------------------------------------------------- #
# .aimstpl 包格式与安全（12.3 / 12.5 第三步 / 12.9）
# --------------------------------------------------------------------------- #
def _pkg_bytes(**over):
    manifest = {
        "uid": "test.pkg-001", "name": "包测试模板", "category": "设备台账",
        "version": "1.0.0", "min_aims_version": "0.5.0", "author": "tester",
    }
    manifest.update(over.get("manifest", {}))
    template = over.get("template", {"devices": [{"device_code": "PKG-{n}", "name": "包设备 {n}",
                                                  "components": [{"component_name": "温度", "unit": "℃"}]}],
                                     "count": 2})
    return build_package(manifest=manifest, template=template,
                         readme=over.get("readme", "说明"))


def test_package_roundtrip():
    data = _pkg_bytes()
    pkg = parse_package(data)
    assert pkg.manifest["uid"] == "test.pkg-001"
    assert pkg.template["count"] == 2
    assert pkg.sha256 and len(pkg.sha256) == 64
    # 重新导出再解析
    data2 = build_package(manifest=pkg.manifest, template=pkg.template)
    assert parse_package(data2).manifest["uid"] == "test.pkg-001"


def test_package_security_checks():
    # 缺 manifest
    with pytest.raises(TemplatePackageError, match="manifest.json"):
        parse_package(_zip({"template.json": "{}"}))
    # 坏 JSON
    bad = _zip({"manifest.json": "{not json", "template.json": "{}"})
    with pytest.raises(TemplatePackageError, match="JSON"):
        parse_package(bad)
    # 缺必需字段（直接构造原始 zip，绕开 build_package 的自检）
    raw = _zip({"manifest.json": json.dumps({"uid": "test.ncat", "name": "x", "category": "",
                                             "version": "1.0.0", "min_aims_version": "0.5.0"}),
                "template.json": "{}"})
    with pytest.raises(TemplatePackageError, match="模板分类"):
        parse_package(raw)
    # 非法 UID
    with pytest.raises(TemplatePackageError, match="唯一 ID"):
        parse_package(_pkg_bytes(manifest={"uid": "bad uid!"}))
    # 版本不兼容 → 拒绝（计划书：检查版本兼容性）
    with pytest.raises(TemplatePackageError, match="不满足"):
        parse_package(_pkg_bytes(manifest={"min_aims_version": "99.0.0"}))
    # 非法 min_aims_version
    with pytest.raises(TemplatePackageError, match="不是合法版本号"):
        parse_package(_pkg_bytes(manifest={"min_aims_version": "abc"}))


def _zip(files: dict, *, traversal: bool = False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr("../evil/" + name if traversal else name, data)
    return buf.getvalue()


def test_zip_slip_rejected():
    """路径穿越攻击必须被拒绝（12.5 第三步）。"""
    data = _zip({"manifest.json": json.dumps({"uid": "test.slip", "name": "x", "category": "设备台账",
                                              "version": "1.0.0", "min_aims_version": "0.5.0"}),
                 "template.json": "{}"}, traversal=True)
    with pytest.raises(TemplatePackageError, match="穿越"):
        parse_package(data)


def test_zip_bomb_rejected(monkeypatch):
    """解压后体积超限应被拒绝（把上限调小以便测试快速执行）。"""
    from app.services import template_package as tp

    monkeypatch.setattr(tp, "MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    big = b"A" * (4 * 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"uid": "test.bomb", "name": "x", "category": "设备台账",
                                                 "version": "1.0.0", "min_aims_version": "0.5.0"}))
        zf.writestr("template.json", big)
    with pytest.raises(TemplatePackageError, match="炸弹|庞大"):
        tp.parse_package(buf.getvalue())


def test_scripts_stored_but_disabled():
    """scripts/ 可以随包存在，但必须标记为禁用（12.9-1/2）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"uid": "test.scr", "name": "x", "category": "设备台账",
                                                 "version": "1.0.0", "min_aims_version": "0.5.0"}))
        zf.writestr("template.json", "{}")
        zf.writestr("scripts/run.py", "import os; os.system('rm -rf /')")
    pkg = parse_package(buf.getvalue())
    assert "run.py" in pkg.scripts
    # 应用/导入后 scripts_enabled 恒为 False
    assert pkg.manifest.get("scripts_enabled", False) is False


# --------------------------------------------------------------------------- #
# 导入流程（12.5）：预览 → 确认 → 冲突处理
# --------------------------------------------------------------------------- #
def _upload(api: _Api, filename: str, data: bytes, confirm: bool):
    return api.post(f"/templates/import?confirm={'true' if confirm else 'false'}",
                    files={"files": (filename, data, "application/zip")})


def test_import_preview_confirm_roundtrip(api: _Api):
    data = _pkg_bytes(manifest={"uid": "test.import-rt"})
    # 预览（不落库）
    resp = _upload(api, "pkg.aimstpl", data, confirm=False)
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is True
    assert result["manifest"]["uid"] == "test.import-rt"
    assert result["scripts"]["enabled"] is False
    # 确认导入
    resp = _upload(api, "pkg.aimstpl", data, confirm=True)
    result = resp.json()["results"][0]
    assert result["ok"] is True, result
    tid = result["summary"]["template_id"]
    row = api.get(f"/templates/{tid}").json()
    assert row["source"] == "imported" and row["template_uid"] == "test.import-rt"
    # 再次导入同 UID：预览应提示冲突为"追加版本"，确认后旧版本保留
    resp = _upload(api, "pkg.aimstpl", data, confirm=False)
    assert "追加为新版本" in resp.json()["results"][0]["conflicts"][0]["detail"]
    resp = _upload(api, "pkg.aimstpl", data, confirm=True)
    tid2 = resp.json()["results"][0]["summary"]["template_id"]
    assert tid2 == tid
    versions = api.get(f"/templates/{tid}/versions").json()
    assert len(versions) == 2, "同 UID 二次导入应追加版本而非覆盖"
    # 导出往返：导出 → 再导入 → 内容一致
    export = api.get(f"/templates/{tid}/export")
    assert export.status_code == 200 and export.headers["content-type"].startswith("application/zip")
    reimport = _upload(api, "rt2.aimstpl", export.content, confirm=True)
    assert reimport.json()["results"][0]["ok"] is True
    # 清理
    api.delete(f"/templates/{tid}")


def test_import_bundle_zip(api: _Api):
    """批量 ZIP（内含多个 .aimstpl）一次导入。"""
    a = _pkg_bytes(manifest={"uid": "test.bundle-a", "name": "批量A", "category": "设备台账"})
    b = _pkg_bytes(manifest={"uid": "test.bundle-b", "name": "批量B", "category": "报表",
                             "template": {"report": {"title": "x"}}})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.aimstpl", a)
        zf.writestr("b.aimstpl", b)
    resp = _upload(api, "bundle.zip", buf.getvalue(), confirm=True)
    results = resp.json()["results"]
    assert len(results) == 2 and all(r["ok"] for r in results)
    for r in results:
        if r.get("summary", {}).get("template_id"):
            api.delete(f"/templates/{r['summary']['template_id']}")


def test_import_failure_logged_with_reason(api: _Api):
    bad = _zip({"manifest.json": "{broken", "template.json": "{}"})
    resp = _upload(api, "bad.aimstpl", bad, confirm=True)
    result = resp.json()["results"][0]
    assert result["ok"] is False and "JSON" in result["error"]
    logs = api.get("/templates/import/logs").json()
    failed = [l for l in logs if l["filename"] == "bad.aimstpl" and l["status"] == "failed"]
    assert failed and failed[0]["reason"], "导入失败必须留痕并给出原因"


def test_import_incompatible_version_rejected(api: _Api):
    data = _pkg_bytes(manifest={"uid": "test.future", "min_aims_version": "999.0.0"})
    resp = _upload(api, "future.aimstpl", data, confirm=False)
    result = resp.json()["results"][0]
    assert result["ok"] is False and "不满足" in result["error"]


# --------------------------------------------------------------------------- #
# 应用模板（12.6）：生成真实业务数据
# --------------------------------------------------------------------------- #
def test_apply_device_template_creates_devices(api: _Api):
    tpl = api.post("/templates", json={
        "name": "应用测试设备模板", "category": "设备台账",
        "content": {"devices": [{"device_code": "APPLY-DEV-{n}", "name": "应用设备 {n}",
                                 "protocol": "Modbus TCP",
                                 "components": [{"component_name": "温度", "unit": "℃",
                                                 "low_limit": 0, "high_limit": 100}]}],
                    "count": 2},
    }).json()
    resp = api.post(f"/templates/{tpl['id']}/apply", json={})
    assert resp.status_code == 200, resp.text
    created = resp.json()["result"]["created"]["devices"]
    assert sorted(created) == ["APPLY-DEV-1", "APPLY-DEV-2"]
    assert resp.json()["result"]["created"]["components"] == 2
    # 再应用一次：幂等（已存在跳过），不报错、不重复创建
    resp2 = api.post(f"/templates/{tpl['id']}/apply", json={})
    assert resp2.status_code == 200
    assert resp2.json()["result"]["created"]["devices"] == []
    # 使用留痕
    usage = api.get(f"/templates/{tpl['id']}/usage").json()
    assert any(u["action"] == "apply" for u in usage)
    # 删除模板不影响业务数据
    api.delete(f"/templates/{tpl['id']}")


def test_apply_alarm_template_creates_rules(api: _Api):
    """设备有测点（组件）时，报警模板按上限生成真实阈值规则。"""
    dev = api.post("/devices", json={"device_code": "APPLY-ALM-1", "name": "报警应用设备"}).json()
    comp = api.post(f"/devices/{dev['id']}/components",
                    json={"component_name": "温度", "unit": "℃", "high_limit": 80}).json()
    assert comp, comp
    tpl = api.post("/templates", json={
        "name": "报警应用模板", "category": "报警配置",
        "content": {"alarm_rules": [{"rule_type": "threshold", "severity": "warning",
                                     "match_component": "*", "high_limit_delta": 10}]},
    }).json()
    resp = api.post(f"/templates/{tpl['id']}/apply", json={"device_code": "APPLY-ALM-1"})
    assert resp.status_code == 200, resp.text
    created = resp.json()["result"]["created"]["alarm_rules"]
    assert created == ["APPLY-ALM-1/温度/warning"]
    # 设备没有组件的分支：新设备 + 无组件 → 不报错、不建规则（宁缺勿滥）
    dev2 = api.post("/devices", json={"device_code": "APPLY-ALM-EMPTY", "name": "空设备"}).json()
    resp2 = api.post(f"/templates/{tpl['id']}/apply", json={"device_code": "APPLY-ALM-EMPTY"})
    assert resp2.status_code == 200 and resp2.json()["result"]["created"]["alarm_rules"] == []
    api.delete(f"/templates/{tpl['id']}")


def test_apply_unsupported_category_refused_honestly(api: _Api):
    """计划书：不允许假成功。不支持自动应用的分类必须明确拒绝。"""
    tpl = api.post("/templates", json={
        "name": "图表应用测试", "category": "图表",
        "content": {"chart": {"type": "line"}},
    }).json()
    resp = api.post(f"/templates/{tpl['id']}/apply", json={})
    assert resp.status_code == 400
    assert "暂不支持自动应用" in resp.json()["detail"]
    api.delete(f"/templates/{tpl['id']}")


def test_delete_template_keeps_business_data(api: _Api):
    """12.9-8：模板删除不得误删已有业务数据。"""
    tpl = api.post("/templates", json={
        "name": "删除隔离测试", "category": "设备台账",
        "content": {"devices": [{"device_code": "KEEP-DEV-1", "name": "保留设备"}], "count": 1},
    }).json()
    api.post(f"/templates/{tpl['id']}/apply", json={})
    api.delete(f"/templates/{tpl['id']}")
    # 设备仍在
    devs = api.get("/devices", params={"q": "KEEP-DEV-1"}).json()
    assert any(d.get("device_code") == "KEEP-DEV-1" for d in devs), "应用生成的设备必须保留"


# --------------------------------------------------------------------------- #
# 导出（12.4-D）
# --------------------------------------------------------------------------- #
def test_batch_export(api: _Api):
    a = api.post("/templates", json={"name": "批量导出A", "category": "设备台账",
                                     "content": {"devices": []}}).json()
    b = api.post("/templates", json={"name": "批量导出B", "category": "报表",
                                     "content": {"report": {"title": "x"}}}).json()
    resp = api.post("/templates/export/batch", json={"ids": [a["id"], b["id"]]})
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) == 2 and all(n.endswith(".aimstpl") for n in names)
    for n in names:
        parse_package(zf.read(n))  # 每个包都能被解析
    api.delete(f"/templates/{a['id']}")
    api.delete(f"/templates/{b['id']}")
