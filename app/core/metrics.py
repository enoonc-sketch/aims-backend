"""进程内指标采集 + Prometheus 文本导出（总方案 8 章"指标导出"遗留项）。

设计取舍：本项目是**单进程** uvicorn（桌面/单机部署），装 Prometheus 客户端库
（prometheus_client）只为几个计数器不值当，还会给 EXE 增加依赖与打包风险。这里用
几十行标准库实现同样的语义：

  * 计数器：请求总数、按状态类 / 方法 / 路由模板分组、5xx 错误数
  * 直方图：请求耗时（固定桶，用于估算分位数）
  * 瞬时值：当前并发请求数、进程 RSS、线程数、运行时长

路由标签做归一化（`/devices/123` → `/devices/{id}`），否则每个 ID 都会生成一条
时间序列，导出时会因为标签基数爆炸而失去意义——这也是指标实现里最常见的坑。
导出端点是 `/api/v1/system/metrics`，仅超管可读（含进程信息，属运维数据）。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

# 延迟桶（毫秒）：覆盖局域网 API 的常见区间，最右桶即"≥10s"
LATENCY_BUCKETS_MS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

logger = logging.getLogger("aims.metrics")

# 超过该耗时记为 WARNING（局域网 API 正常在百毫秒内，3 秒已是"用户在等"）
SLOW_REQUEST_MS = 3000.0

_NUMERIC_SEG = re.compile(r"/\d+(?=/|$)")
_UUID_SEG = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)")


def normalize_path(path: str) -> str:
    """把路径里的 ID 归一化成模板，控制标签基数。

    `/api/v1/devices/12/measurements?x=1` → `/api/v1/devices/{id}/measurements`
    """
    if not path:
        return "/"
    clean = path.split("?", 1)[0]
    clean = _UUID_SEG.sub("/{uuid}", clean)
    clean = _NUMERIC_SEG.sub("/{id}", clean)
    return clean[:200]


def _status_class(status: int) -> str:
    if status >= 500:
        return "5xx"
    if status >= 400:
        return "4xx"
    if status >= 300:
        return "3xx"
    if status >= 200:
        return "2xx"
    return "1xx"


class MetricsRegistry:
    """线程安全的进程内指标。写入发生在事件循环线程，读取可能在 to_thread 里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at = time.monotonic()
        self.started_wall = datetime.now(timezone.utc)
        self.requests_total = 0
        self.by_status: dict[str, int] = defaultdict(int)
        self.by_method: dict[str, int] = defaultdict(int)
        self.by_route: dict[str, int] = defaultdict(int)
        self.errors_total = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.latency_count = 0
        self.latency_sum_ms = 0.0
        self.latency_max_ms = 0.0
        # 每个桶累计"≤该桶上限"的次数，与 Prometheus 的 le 语义一致
        self.latency_buckets: dict[float, int] = {b: 0 for b in LATENCY_BUCKETS_MS}
        self.latency_overflow = 0

    # ---------------- 写入 ----------------
    def request_started(self) -> None:
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight

    def observe_request(self, method: str, path: str, status: int, duration_ms: float) -> None:
        route = normalize_path(path)
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.requests_total += 1
            self.by_status[_status_class(status)] += 1
            self.by_method[method.upper()] += 1
            self.by_route[route] += 1
            if status >= 500:
                self.errors_total += 1
            self.latency_count += 1
            self.latency_sum_ms += duration_ms
            if duration_ms > self.latency_max_ms:
                self.latency_max_ms = duration_ms
            for bound in LATENCY_BUCKETS_MS:
                if duration_ms <= bound:
                    self.latency_buckets[bound] += 1
            if duration_ms > LATENCY_BUCKETS_MS[-1]:
                self.latency_overflow += 1

    def reset(self) -> None:
        """测试用：清空计数，保留启动时间语义不重要的部分。"""
        with self._lock:
            self.__init__()  # type: ignore[misc]

    # ---------------- 读取 ----------------
    def _quantile_locked(self, q: float) -> float:
        """由直方图估算分位数（线性插值到桶内，误差 ≤ 桶宽）。

        样本全部落在最左桶时，插值会外推到桶上限（例如实测最慢 1.5 ms 却报 p99=5 ms）。
        估算值必须夹在实测最大值以内，否则监控上会出现"分位数高于最大值"这种假异常。
        """
        if self.latency_count == 0:
            return 0.0
        target = q * self.latency_count
        prev_bound = 0.0
        prev_count = 0
        estimate = float(self.latency_max_ms)
        for bound in LATENCY_BUCKETS_MS:
            cumulative = self.latency_buckets[bound]
            if cumulative >= target:
                in_bucket = cumulative - prev_count
                if in_bucket <= 0:
                    estimate = float(bound)
                else:
                    ratio = (target - prev_count) / in_bucket
                    estimate = prev_bound + (bound - prev_bound) * ratio
                break
            prev_bound, prev_count = float(bound), cumulative
        return round(min(estimate, self.latency_max_ms), 2)

    def snapshot(self, *, top_routes: int = 15) -> dict:
        with self._lock:
            routes = sorted(self.by_route.items(), key=lambda kv: kv[1], reverse=True)[:top_routes]
            data = {
                "started_at": self.started_wall.isoformat(timespec="seconds"),
                "uptime_seconds": round(time.monotonic() - self.started_at, 1),
                "requests": {
                    "total": self.requests_total,
                    "errors_5xx": self.errors_total,
                    "in_flight": self.in_flight,
                    "max_in_flight": self.max_in_flight,
                    "by_status": dict(sorted(self.by_status.items())),
                    "by_method": dict(sorted(self.by_method.items())),
                    "top_routes": [{"route": r, "count": c} for r, c in routes],
                },
                "latency_ms": {
                    "count": self.latency_count,
                    "avg": round(self.latency_sum_ms / self.latency_count, 2) if self.latency_count else 0.0,
                    "p50": self._quantile_locked(0.50),
                    "p90": self._quantile_locked(0.90),
                    "p95": self._quantile_locked(0.95),
                    "p99": self._quantile_locked(0.99),
                    "max": round(self.latency_max_ms, 2),
                },
                "process": process_stats(),
            }
        return data

    def prometheus(self) -> str:
        snap = self.snapshot()
        req, lat, proc = snap["requests"], snap["latency_ms"], snap["process"]
        lines = [
            "# HELP aims_uptime_seconds Seconds since the process started serving.",
            "# TYPE aims_uptime_seconds gauge",
            f"aims_uptime_seconds {snap['uptime_seconds']}",
            "# HELP aims_requests_total Total HTTP requests observed.",
            "# TYPE aims_requests_total counter",
            f"aims_requests_total {req['total']}",
            "# HELP aims_requests_by_status HTTP requests grouped by status class.",
            "# TYPE aims_requests_by_status counter",
        ]
        lines += [f'aims_requests_by_status{{status="{k}"}} {v}' for k, v in req["by_status"].items()]
        lines += [
            "# HELP aims_requests_by_method HTTP requests grouped by method.",
            "# TYPE aims_requests_by_method counter",
        ]
        lines += [f'aims_requests_by_method{{method="{k}"}} {v}' for k, v in req["by_method"].items()]
        lines += [
            "# HELP aims_http_errors_total Responses with status >= 500.",
            "# TYPE aims_http_errors_total counter",
            f"aims_http_errors_total {req['errors_5xx']}",
            "# HELP aims_http_requests_in_flight Requests currently being processed.",
            "# TYPE aims_http_requests_in_flight gauge",
            f"aims_http_requests_in_flight {req['in_flight']}",
            "# HELP aims_request_duration_ms Request latency in milliseconds.",
            "# TYPE aims_request_duration_ms histogram",
        ]
        for bound in LATENCY_BUCKETS_MS:
            label = int(bound) if float(bound).is_integer() else bound
            lines.append(f'aims_request_duration_ms_bucket{{le="{label}"}} {self.latency_buckets[bound]}')
        lines += [
            f'aims_request_duration_ms_bucket{{le="+Inf"}} {lat["count"]}',
            f"aims_request_duration_ms_sum {round(self.latency_sum_ms, 3)}",
            f"aims_request_duration_ms_count {lat['count']}",
            "# HELP aims_process_resident_memory_bytes Resident set size of the API process.",
            "# TYPE aims_process_resident_memory_bytes gauge",
            f"aims_process_resident_memory_bytes {proc.get('rss_bytes', 0)}",
            "# HELP aims_process_threads Thread count of the API process.",
            "# TYPE aims_process_threads gauge",
            f"aims_process_threads {proc.get('threads', 0)}",
        ]
        return "\n".join(lines) + "\n"


def process_stats() -> dict:
    """进程级信息：RSS / 线程数 / pid。取不到就如实留空，不假装有数据。"""
    stats: dict = {"pid": os.getpid(), "threads": threading.active_count()}
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    stats["rss_bytes"] = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    if "rss_bytes" not in stats:  # Windows / macOS 兜底
        try:
            import resource  # type: ignore[import-not-found]

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux 单位是 KB，macOS 是字节
            stats["rss_bytes"] = int(usage if usage > 10**7 else usage * 1024)
        except Exception:  # pragma: no cover - 平台相关
            stats["rss_bytes"] = 0
    return stats


REGISTRY = MetricsRegistry()


class MetricsMiddleware:
    """纯 ASGI 指标中间件：计时、计数、绑定日志上下文。

    放在最外层，这样被安全头/限流直接拒绝的请求（413/429/401）也会被统计到——
    现场排障最想知道的恰恰是"哪一类请求在被打回"。
    """

    def __init__(self, app, api_prefix: str = "/api/v1", registry: MetricsRegistry | None = None):
        self.app = app
        self.api_prefix = api_prefix
        self.registry = registry or REGISTRY

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        from app.core.logging_setup import bind_request_context, reset_request_context

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        status = {"code": 500}
        self.registry.request_started()
        request_id = f"{int(time.time() * 1000):x}-{id(scope) & 0xFFFF:04x}"
        token = bind_request_context(request_id=request_id, method=method, path=normalize_path(path))

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        started = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.registry.observe_request(method, path, status["code"], elapsed_ms)
            # 只在"值得看"的时候写一行：5xx 全记（现场排障的主角），慢请求记 WARNING。
            # 不逐条记录 2xx，避免把日志变成访问流水（单机部署没人会去读 10 万行流水）。
            # 显式带 extra：即使日志被队列/异步 handler 延迟格式化（contextvar 已复位），
            # 关联字段也还在 record 上，不会丢。
            ctx_extra = {"request_id": request_id, "method": method,
                         "path": normalize_path(path), "status": status["code"],
                         "duration_ms": round(elapsed_ms, 1)}
            if status["code"] >= 500:
                logger.error("request failed: %s %s -> %s (%.1f ms)",
                             method, normalize_path(path), status["code"], elapsed_ms,
                             extra=ctx_extra)
            elif elapsed_ms >= SLOW_REQUEST_MS:
                logger.warning("slow request: %s %s -> %s (%.1f ms, threshold %.0f ms)",
                               method, normalize_path(path), status["code"], elapsed_ms,
                               SLOW_REQUEST_MS, extra=ctx_extra)
            reset_request_context(token)
