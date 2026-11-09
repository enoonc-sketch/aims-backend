"""Pure-ASGI security middleware (no dependency on Starlette internals).

  * HTTPSEnforcementMiddleware  - refuse plaintext HTTP when HTTPS is required
  * SecurityHeadersMiddleware   - nosniff / frame-deny / no-referrer / HSTS / no-store
  * RequestSizeLimitMiddleware  - cap request bodies (Content-Length AND streamed bytes)
  * AuditMiddleware             - record every state-changing API call

Recommended stacking (outermost -> innermost): HTTPS -> TrustedHost -> SecurityHeaders
-> CORS -> Audit -> RequestSizeLimit -> app.  Audit sits outside the size limiter so a
413 produced by the limiter is what gets audited.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Iterable

from app.core import audit_context

logger = logging.getLogger("aims.security")

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _json_response_messages(status: int, detail: str, extra_headers: Iterable[tuple[bytes, bytes]] = ()):
    body = json.dumps({"detail": detail}).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers.extend(extra_headers)
    return (
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    )


def client_ip_from_scope(scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


# --------------------------------------------------------------------------- #
class HTTPSEnforcementMiddleware:
    """Reject plaintext requests. Redirecting is deliberately NOT done (it would
    require trusting the Host header, and credentials would already have crossed the
    wire in clear); the TLS-terminating proxy is responsible for 80 -> 443.
    `scope["scheme"]` is only rewritten from X-Forwarded-Proto by uvicorn for peers
    listed in --forwarded-allow-ips, so a client cannot spoof it."""

    def __init__(self, app, exempt_paths: Iterable[str] = ("/health",)):
        self.app = app
        self.exempt = set(exempt_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("scheme") not in ("https", "wss"):
            if scope["type"] == "http" and scope.get("path") not in self.exempt:
                start, body = _json_response_messages(403, "HTTPS is required")
                await send(start)
                await send(body)
                return
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
class SecurityHeadersMiddleware:
    def __init__(self, app, hsts: bool = False, api_prefix: str = "/api"):
        self.app = app
        self.hsts = hsts
        self.api_prefix = api_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_api = scope.get("path", "").startswith(self.api_prefix)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}

                def add(name: bytes, value: bytes):
                    if name not in present:
                        headers.append((name, value))

                add(b"x-content-type-options", b"nosniff")
                add(b"x-frame-options", b"DENY")
                add(b"referrer-policy", b"no-referrer")
                if is_api:
                    add(b"cache-control", b"no-store")
                if self.hsts:
                    add(b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------- #
class _BodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Enforce a maximum request body size *before* the framework buffers it.

    Honest clients are refused from the Content-Length header alone; clients that
    omit or lie about it are cut off as soon as the streamed byte count exceeds the
    limit. (FastAPI would otherwise convert a mid-parse failure into a generic 400,
    so the 413 is written here, directly.)"""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        msg = f"Request body too large (max {self.max_bytes // (1024 * 1024)} MB)"
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > self.max_bytes:
                        start, body = _json_response_messages(413, msg)
                        await send(start)
                        await send(body)
                        return
                except ValueError:
                    start, body = _json_response_messages(400, "Invalid Content-Length")
                    await send(start)
                    await send(body)
                    return
                break

        total = 0
        rejected = False
        started = False

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return  # the app's own (400) reaction to our cut-off is discarded
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive():
            nonlocal total, rejected
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    if not started and not rejected:
                        rejected = True
                        start, body = _json_response_messages(413, msg, [(b"connection", b"close")])
                        await send(start)
                        await send(body)
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        except Exception:
            if not rejected:
                raise


# --------------------------------------------------------------------------- #
AuditWriter = Callable[[dict, dict, int, float], None]


class AuditMiddleware:
    """Write one audit row per state-changing API request.

    Paths in `skip_paths` are audited explicitly by their own endpoints (login,
    refresh, logout) with richer detail, so they are not double-logged here.
    A failure to write the audit row is logged loudly but does not turn a
    completed business operation into a 500."""

    def __init__(self, app, api_prefix: str, writer: AuditWriter, skip_paths: Iterable[str] = ()):
        self.app = app
        self.api_prefix = api_prefix
        self.writer = writer
        self.skip = set(skip_paths)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] not in _MUTATING
            or not scope.get("path", "").startswith(self.api_prefix)
            or scope.get("path") in self.skip
        ):
            return await self.app(scope, receive, send)

        ctx, token = audit_context.begin()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        started = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            audit_context.end(token)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            try:
                await asyncio.to_thread(self.writer, scope, ctx, status["code"], elapsed_ms)
            except Exception:  # pragma: no cover - defensive
                logger.exception("AUDIT WRITE FAILED for %s %s", scope.get("method"), scope.get("path"))


def make_db_audit_writer() -> AuditWriter:
    """Default writer: persists to `audit_logs` using the app's DB dependency (so
    test/dependency overrides of get_db apply to auditing as well)."""

    def writer(scope: dict, ctx: dict, status_code: int, elapsed_ms: float) -> None:
        from app.db.session import get_db
        from app.services.audit import record_audit

        app = scope.get("app")
        override = getattr(app, "dependency_overrides", {}).get(get_db) if app is not None else None
        gen = (override or get_db)()
        db = next(gen)
        try:
            user_id = ctx.get("user_id")
            if user_id is None:
                user_id = _user_id_from_bearer(scope)
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            detail: dict[str, Any] = {
                "method": scope["method"],
                "path": scope["path"][:300],
                "status": status_code,
                "outcome": "success" if status_code < 400 else ("denied" if status_code in (401, 403) else "failed"),
                "duration_ms": round(elapsed_ms, 1),
                "user_agent": headers.get(b"user-agent", b"").decode("latin-1")[:200],
            }
            if "upload" in ctx:
                detail["upload"] = ctx["upload"]
            if "audit_detail" in ctx:
                detail.update(ctx["audit_detail"])
            record_audit(
                db,
                user_id=user_id,
                action=f"API_{scope['method']}",
                entity=scope["path"][:100],
                detail=detail,
                ip_address=client_ip_from_scope(scope),
            )
            db.commit()
        except Exception:
            logger.exception("Could not persist audit record")
            try:
                db.rollback()
            except Exception:  # pragma: no cover
                pass
        finally:
            gen.close()

    return writer


def _user_id_from_bearer(scope) -> int | None:
    from app.core.security import decode_access_token

    for k, v in scope.get("headers", []):
        if k == b"authorization":
            parts = v.decode("latin-1").split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                claims = decode_access_token(parts[1].strip())
                if claims:
                    try:
                        return int(claims["sub"])
                    except (KeyError, ValueError):
                        return None
    return None
