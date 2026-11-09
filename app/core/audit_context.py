"""Per-request scratchpad shared between endpoints/dependencies and the audit
middleware.

The middleware installs a mutable dict in a ContextVar *before* the request is
handled. Anything running downstream (dependencies, endpoints, threadpool
workers — they all inherit a copy of the context, which still points at the same
dict object) can add facts with `note(...)`; the middleware reads them back once
the response has been produced. Nothing here touches the database.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Optional

_current: ContextVar[Optional[dict[str, Any]]] = ContextVar("aims_audit_ctx", default=None)


def begin() -> tuple[dict[str, Any], Token]:
    data: dict[str, Any] = {}
    return data, _current.set(data)


def end(token: Token) -> None:
    _current.reset(token)


def note(**facts: Any) -> None:
    """Attach facts (user_id=..., upload={...}, ...) to the current request."""
    data = _current.get()
    if data is not None:
        data.update(facts)


def get(key: str, default: Any = None) -> Any:
    data = _current.get()
    return default if data is None else data.get(key, default)
