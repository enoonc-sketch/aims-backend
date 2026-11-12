"""功能完整性检测中发现的两个真实缺陷的回归测试。

1. POST /devices/{id}/components 给"新 TAG"绑定 网关+地址 时返回 500：
   get_or_create_tag 内部已经 link_gateway（待写入、未 flush），接口里再 link 一次，
   会话又是 autoflush=False，db.get() 看不到待写入对象，于是重复 INSERT 撞
   gateway_devices 唯一约束。
2. 云数据源 test-login / 刷新：Playwright 遇到证书错误、域名解析失败、连接被拒时抛的是
   通用 playwright Error（不是 TimeoutError），原先没捕获，接口变成 500 + 原始堆栈；
   函数文档明确承诺"任何失败都转成 ScraperError"。
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_user, get_db
from app.main import app


@pytest.fixture()
def prod_client(db_session, test_user):
    """和生产一致的会话配置（autoflush=False）。

    conftest 里的 db_session 用的是 sessionmaker 默认值 autoflush=True，会把"待写入对象
    查不到"这一类缺陷整个掩盖掉——第 1 个缺陷在 618 个测试里一直没被发现就是这个原因。
    """
    session = sessionmaker(bind=db_session.get_bind(), autoflush=False, future=True)()
    app.dependency_overrides[get_db] = lambda: (yield session)
    app.dependency_overrides[get_current_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        session.close()


def _mk_device(client, code="REG-001"):
    r = client.post("/api/v1/devices", json={"device_code": code, "name": "reg"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _mk_gateway(client, code="GW-REG"):
    r = client.post("/api/v1/gateways", json={"gateway_code": code, "name": "reg gw"})
    assert r.status_code in (200, 201), r.text
    return code


def test_configure_new_tag_with_gateway_does_not_500(prod_client):
    did = _mk_device(prod_client)
    gw = _mk_gateway(prod_client)
    r = prod_client.post(
        f"/api/v1/devices/{did}/components",
        json={"component_name": "CO2", "gateway_code": gw, "tag_address": "40001"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["gateway_code"] == gw and r.json()["tag_address"] == "40001"
    # 同一设备、同一网关再配第二个 TAG：关联已存在，不能再插一条
    r2 = prod_client.post(
        f"/api/v1/devices/{did}/components",
        json={"component_name": "O2", "gateway_code": gw, "tag_address": "40002"},
    )
    assert r2.status_code == 201, r2.text


def test_configure_existing_tag_then_bind_gateway(prod_client):
    did = _mk_device(prod_client, "REG-002")
    gw = _mk_gateway(prod_client, "GW-REG2")
    assert prod_client.post(f"/api/v1/devices/{did}/components", json={"component_name": "H2S"}).status_code == 201
    r = prod_client.post(
        f"/api/v1/devices/{did}/components",
        json={"component_name": "H2S", "gateway_code": gw, "tag_address": "40010"},
    )
    assert r.status_code == 201, r.text


def test_ingest_after_binding_reaches_measurements(prod_client):
    did = _mk_device(prod_client, "REG-003")
    gw = _mk_gateway(prod_client, "GW-REG3")
    prod_client.post(
        f"/api/v1/devices/{did}/components",
        json={"component_name": "CH4", "gateway_code": gw, "tag_address": "30001", "high_limit": 10},
    )
    r = prod_client.post(
        "/api/v1/communication/ingest",
        json={"gateway_code": gw, "points": [{"tag_address": "30001", "value": 3.5}, {"tag_address": "nope", "value": 1}]},
    )
    assert r.status_code in (200, 201, 202), r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["unmapped_addresses"]


# ------------------------------------------------------------------ 抓取器
def _install_fake_playwright(monkeypatch, goto_exc):
    class PWError(Exception):
        pass

    class PWTimeoutError(PWError):
        pass

    class _Page:
        def goto(self, *a, **k):
            raise goto_exc(PWError, PWTimeoutError)

    class _Ctx:
        def new_page(self):
            return _Page()

        def close(self):
            pass

    class _Browser:
        def new_context(self, **k):
            return _Ctx()

        def close(self):
            pass

    class _Chromium:
        def launch(self, **k):
            return _Browser()

    class _PW:
        chromium = _Chromium()

    @contextmanager
    def sync_playwright():
        yield _PW()

    mod = types.ModuleType("playwright.sync_api")
    mod.Error, mod.TimeoutError, mod.sync_playwright = PWError, PWTimeoutError, sync_playwright
    pkg = types.ModuleType("playwright")
    pkg.sync_api = mod
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)


@pytest.mark.parametrize(
    "msg",
    [
        "Page.goto: net::ERR_CERT_COMMON_NAME_INVALID at https://x/login\nCall log:\n  - navigating",
        "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x/login",
        "Page.goto: net::ERR_CONNECTION_REFUSED at https://x/login",
    ],
)
def test_generic_playwright_error_becomes_scraper_error(monkeypatch, msg):
    from app.services import external_scraper as sc

    _install_fake_playwright(monkeypatch, lambda PWError, _T: PWError(msg))
    monkeypatch.setattr(sc, "decrypt_secret", lambda _v: "pw")
    src = types.SimpleNamespace(
        name="Vendor", login_url="https://x/login", username="u", password_encrypted="e",
        username_selector="#u", password_selector="#p", submit_selector="#s", login_success_selector=None,
    )
    with pytest.raises(sc.ScraperError) as ei:
        with sc.external_session(src):
            pass
    text = str(ei.value)
    assert "Vendor" in text and "login_url" in text
    assert "Call log" not in text          # 只给第一行，不把多行调试输出甩给界面
