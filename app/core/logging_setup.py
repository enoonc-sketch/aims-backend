"""结构化日志：文本 / JSON 双格式，带请求上下文（总方案 8 章"现场排障"遗留项）。

现场部署是单机 Windows，默认仍输出人类可读的文本；设置 `AIMS_LOG_FORMAT=json`
后每条日志压成一行 JSON，字段固定、可直接被 `jq` / Filebeat / Loki / ELK 采集，
不需要再为排障写正则去解析半结构化文本。

固定字段：
    ts           ISO8601 UTC 时间戳（毫秒精度）
    level        DEBUG/INFO/WARNING/ERROR/CRITICAL
    logger       日志器名（aims、aims.security …）
    message      格式化后的消息（%s 参数已展开）
    request_id   当前请求的关联 ID（无请求上下文时省略）
    method/path  当前请求的方法与路径（同上）
    user_id      已认证用户（同上）
    duration_ms  请求耗时（同上）
    exc          异常栈（有 exc_info 时）

请求级字段通过 `bind_request_context()` 注入，落在 contextvar 上，因此并发请求
之间不会串台（asyncio 每个任务各自一份上下文）。"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

# 请求上下文：由 MetricsMiddleware 绑定，日志格式化时自动带上
_REQUEST_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("aims_log_ctx", default={})

# 允许出现在日志行里的上下文字段（白名单，避免把任意 extra 都吐到日志里）
_CONTEXT_KEYS = ("request_id", "method", "path", "status", "duration_ms", "user_id", "event")

_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def bind_request_context(**kwargs) -> contextvars.Token:
    """绑定当前请求的日志上下文，返回可用于 reset 的 token。"""
    ctx = dict(_REQUEST_CTX.get())
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    return _REQUEST_CTX.set(ctx)


def reset_request_context(token: contextvars.Token) -> None:
    try:
        _REQUEST_CTX.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover - 跨任务 reset 的兜底
        _REQUEST_CTX.set({})


def current_request_context() -> dict:
    return dict(_REQUEST_CTX.get())


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。异常栈放在 `exc` 字段，不破坏 JSON 结构。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = _REQUEST_CTX.get()
        for key in _CONTEXT_KEYS:
            value = ctx.get(key)
            if value is not None:
                payload[key] = value
        # 调用方显式传的 extra（如 event="import_done"）也带上，同样走白名单
        for key in _CONTEXT_KEYS:
            if key not in payload and hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None, fmt: str | None = None, force: bool = False) -> None:
    """初始化根日志器（幂等：模块被多次导入也只装一次 handler）。

    环境变量优先于参数：`AIMS_LOG_LEVEL`、`AIMS_LOG_FORMAT`（text|json）。
    """
    level_name = (level or os.environ.get("AIMS_LOG_LEVEL") or "INFO").upper()
    fmt_name = (fmt or os.environ.get("AIMS_LOG_FORMAT") or "text").lower()
    root = logging.getLogger()

    if not force and getattr(root, "_aims_configured", False):
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    if fmt_name == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))

    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root._aims_configured = True  # type: ignore[attr-defined]
