"""阶段 7：请求限流（M-7）。

用最小 ASGI 应用直接挂中间件测试——不 reload 全局 app，避免污染其他测试的
DB/依赖覆盖状态。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import RateLimitMiddleware


def _client(api_limit=3, auth_limit=2, window=60):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    def login():
        return {"token": "x"}

    app.add_middleware(
        RateLimitMiddleware,
        api_prefix="/api/v1",
        auth_paths=("/auth/login",),
        global_limit=api_limit,
        auth_limit=auth_limit,
        window_seconds=window,
    )
    return TestClient(app)


def test_api_bucket_returns_429_with_retry_after():
    c = _client(api_limit=3)
    codes = [c.get("/api/v1/ping").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    r = c.get("/api/v1/ping")
    assert r.headers.get("retry-after") and r.headers.get("x-ratelimit-remaining") == "0"


def test_auth_bucket_is_stricter_and_independent():
    c = _client(api_limit=100, auth_limit=2)
    assert c.post("/api/v1/auth/login").status_code == 200
    assert c.post("/api/v1/auth/login").status_code == 200
    r = c.post("/api/v1/auth/login")
    assert r.status_code == 429 and "频繁" in r.json()["detail"]
    # 普通 API 桶不受认证桶影响
    assert c.get("/api/v1/ping").status_code == 200


def test_health_is_never_limited():
    c = _client(api_limit=1)
    for _ in range(5):
        assert c.get("/health").status_code == 200


def test_window_slides(monkeypatch):
    """窗口过后计数清零（用极短窗口验证，不 sleep 长）。"""
    import time

    c = _client(api_limit=1, window=0.3)
    assert c.get("/api/v1/ping").status_code == 200
    assert c.get("/api/v1/ping").status_code == 429
    time.sleep(0.35)
    assert c.get("/api/v1/ping").status_code == 200
