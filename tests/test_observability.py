"""可观测性测试：结构化日志（JSON 格式）与运行指标端点。

覆盖点：
  * JSON 日志是合法单行 JSON，含固定字段与请求上下文，异常不破坏结构
  * 请求上下文不会串到别的请求（contextvar 隔离）
  * `/system/metrics` 仅超管可读，普通用户 403
  * 指标真实计数（含 4xx/5xx），路由标签做 ID 归一化，不会基数爆炸
  * Prometheus 输出符合抓取格式（bucket 单调、+Inf 等于总数）
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core.logging_setup import JsonFormatter, bind_request_context, reset_request_context
from app.core.metrics import MetricsRegistry, normalize_path
from app.core.security import hash_password
from app.models.system import User
from app.api.deps import get_current_user, get_db
from app.main import app


# ---------------------------------------------------------------- 路由归一化
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/api/v1/devices/12/measurements", "/api/v1/devices/{id}/measurements"),
        ("/api/v1/alarms/9", "/api/v1/alarms/{id}"),
        ("/api/v1/devices", "/api/v1/devices"),
        ("/api/v1/measurements?device_id=3&limit=10", "/api/v1/measurements"),
        ("/api/v1/templates/3f5a1c2e-1111-2222-3333-444455556666/files",
         "/api/v1/templates/{uuid}/files"),
    ],
)
def test_normalize_path_collapses_ids(raw, expected):
    assert normalize_path(raw) == expected


def test_normalize_path_keeps_version_numbers():
    # 版本号是路径语义的一部分，不能被当成 ID 吃掉
    assert normalize_path("/api/v1/system/metrics") == "/api/v1/system/metrics"


# ---------------------------------------------------------------- JSON 日志
def _record(msg: str, level: int = logging.INFO, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(name="aims.test", level=level, pathname=__file__, lineno=1,
                             msg=msg, args=(), exc_info=exc_info)


def test_json_formatter_emits_single_line_valid_json():
    out = JsonFormatter().format(_record("导入完成 %s", ))
    assert "\n" not in out
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "aims.test"
    assert payload["message"]
    assert payload["ts"].endswith("Z") or "+00:00" in payload["ts"]


def test_json_formatter_includes_request_context():
    token = bind_request_context(request_id="abc123", method="POST", path="/api/v1/devices")
    try:
        payload = json.loads(JsonFormatter().format(_record("写入设备")))
    finally:
        reset_request_context(token)
    assert payload["request_id"] == "abc123"
    assert payload["method"] == "POST"
    assert payload["path"] == "/api/v1/devices"

    # reset 之后不应再带上下文（不串到下一个请求）
    payload2 = json.loads(JsonFormatter().format(_record("后续日志")))
    assert "request_id" not in payload2


def test_json_formatter_keeps_exception_in_exc_field():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record("处理失败", level=logging.ERROR, exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in payload["exc"]
    assert payload["level"] == "ERROR"


# ---------------------------------------------------------------- 指标端点
@pytest.fixture()
def normal_user(db_session):
    user = User(
        username="metrics-normal-user",
        hashed_password=hash_password("Normal-Pw-0nly-4-Tests!"),
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture()
def non_admin_client(db_session, normal_user):
    def _override_get_db():
        yield db_session

    def _override_get_current_user():
        return normal_user

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_metrics_requires_superuser(client):
    r = client.get("/api/v1/system/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "requests" in body and "latency_ms" in body and "process" in body
    assert body["process"]["pid"] > 0
    assert isinstance(body["uptime_seconds"], (int, float))


def test_metrics_rejects_normal_user(non_admin_client):
    r = non_admin_client.get("/api/v1/system/metrics")
    assert r.status_code == 403


def test_metrics_rejects_anonymous(anon_client):
    r = anon_client.get("/api/v1/system/metrics")
    assert r.status_code == 401


def test_metrics_counts_requests_and_normalizes_routes(client):
    from app.core.metrics import REGISTRY

    REGISTRY.reset()
    client.get("/health")
    client.get("/api/v1/devices/424242")
    client.get("/api/v1/devices/999999")  # 404 也要统计
    snap = REGISTRY.snapshot()

    assert snap["requests"]["total"] >= 3
    assert snap["requests"]["by_status"].get("2xx", 0) >= 1
    assert snap["requests"]["by_status"].get("4xx", 0) >= 1
    routes = {r["route"]: r["count"] for r in snap["requests"]["top_routes"]}
    assert routes.get("/api/v1/devices/{id}", 0) >= 2
    assert all("/424242" not in r for r in routes), "ID 必须归一到模板，避免标签基数爆炸"


def test_metrics_latency_histogram_is_sane(client):
    from app.core.metrics import REGISTRY

    REGISTRY.reset()
    for _ in range(3):
        client.get("/health")
    latency = REGISTRY.snapshot()["latency_ms"]
    assert latency["count"] == 3
    # 分位数由固定桶插值而来，单调性与上界必须成立
    assert latency["p50"] <= latency["p95"] <= latency["p99"]
    assert latency["max"] >= latency["p99"]
    assert latency["max"] <= 60_000, "单次测试请求不可能超过 60 秒"
    assert latency["avg"] >= 0


def test_prometheus_output_is_parseable(client):
    from app.core.metrics import REGISTRY

    REGISTRY.reset()
    client.get("/health")
    r = client.get("/api/v1/system/metrics?format=prometheus")
    assert r.status_code == 200
    body = r.text
    assert "aims_requests_total 1" in body
    assert "aims_request_duration_ms_count 1" in body

    buckets = [line for line in body.splitlines() if line.startswith("aims_request_duration_ms_bucket")]
    values = [float(line.rsplit(" ", 1)[1]) for line in buckets]
    assert values == sorted(values), "Prometheus 直方图 bucket 必须单调不减"
    assert 'le="+Inf"' in body
    assert values[-1] == 1.0


def test_prometheus_rejects_normal_user(non_admin_client):
    r = non_admin_client.get("/api/v1/system/metrics?format=prometheus")
    assert r.status_code == 403


def test_registry_reset_clears_counters():
    reg = MetricsRegistry()
    reg.request_started()
    reg.observe_request("GET", "/x", 200, 7.0)
    assert reg.snapshot()["requests"]["total"] == 1
    reg.reset()
    assert reg.snapshot()["requests"]["total"] == 0


def test_middleware_logs_5xx_with_request_context():
    """5xx 必须留一行带 request_id 的 ERROR（现场排障靠它把日志串起来）。"""
    import asyncio

    from app.core.metrics import MetricsMiddleware

    async def failing_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"boom"})

    reg = MetricsRegistry()
    mw = MetricsMiddleware(failing_app, registry=reg)

    records: list[logging.LogRecord] = []

    formatted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)
            formatted.append(self.format(record))

    handler = Capture()
    handler.setFormatter(JsonFormatter())
    mw_logger = logging.getLogger("aims.metrics")
    old_level = mw_logger.level
    mw_logger.addHandler(handler)
    mw_logger.setLevel(logging.INFO)
    try:
        scope = {"type": "http", "method": "GET", "path": "/api/v1/devices/7", "headers": []}
        asyncio.run(mw(scope, None, lambda m: asyncio.sleep(0)))
    finally:
        mw_logger.removeHandler(handler)
        mw_logger.setLevel(old_level)

    assert reg.snapshot()["requests"]["errors_5xx"] == 1
    assert records, "5xx 请求必须产生一条 ERROR 日志"
    payload = json.loads(formatted[-1])
    assert payload["level"] == "ERROR"
    assert payload["request_id"], "日志必须带请求关联 ID"
    assert payload["path"] == "/api/v1/devices/{id}", "日志路径同样要做 ID 归一化"


def test_configure_logging_honours_env(monkeypatch):
    """AIMS_LOG_FORMAT=json 必须真的把 handler 换成 JSON 格式器（否则等于没实现）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        monkeypatch.setenv("AIMS_LOG_FORMAT", "json")
        monkeypatch.setenv("AIMS_LOG_LEVEL", "WARNING")
        from app.core.logging_setup import configure_logging

        configure_logging(force=True)
        assert root.handlers, "至少要有一个 handler"
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING

        # 切回文本：再配置一次必须换回普通格式器（幂等 + 可切换）
        monkeypatch.setenv("AIMS_LOG_FORMAT", "text")
        configure_logging(force=True)
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_deploy_env_template_documents_log_switches():
    """安装生成的 aims.env 里要能看到日志开关，否则运维不知道有这个能力。"""
    from app.deploy.conf import build_env, render_env
    from app.deploy.paths import Layout
    from pathlib import Path

    layout = Layout(root=Path("/tmp/aims-test-root"), data_root=Path("/tmp/aims-test-data"))
    values = build_env(layout, {"pg": {"host": "127.0.0.1", "app_user": "aims", "database": "aims"}},
                       {"DB_PASSWORD": "x", "SECRET_KEY": "y", "ADMIN_PASSWORD": "z"}, port=5432, api_port=8000)
    text = render_env(values)
    assert "AIMS_LOG_FORMAT=text" in text
    assert "AIMS_LOG_LEVEL=INFO" in text
