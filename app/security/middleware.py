"""Pure-ASGI security middleware (works for HTTP and WebSocket, no body buffering)."""
from __future__ import annotations

import json
from typing import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import settings
from .rate_limit import limiter

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: https://tile.openstreetmap.org",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])
# Swagger UI (dev only) needs its CDN bundle + an inline bootstrap script.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'none'")


def _hdr(name: str, value: str) -> tuple[bytes, bytes]:
    return name.lower().encode("latin-1"), value.encode("latin-1")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        base = [
            _hdr("X-Content-Type-Options", "nosniff"),
            _hdr("X-Frame-Options", "DENY"),
            # OSM tile servers require a Referer; send only the origin cross-site.
            _hdr("Referrer-Policy", "strict-origin-when-cross-origin"),
            _hdr("Permissions-Policy", "geolocation=(self), camera=(), microphone=(), payment=(), usb=()"),
            _hdr("Cross-Origin-Opener-Policy", "same-origin"),
            _hdr("Cross-Origin-Resource-Policy", "same-origin"),
            _hdr("X-Permitted-Cross-Domain-Policies", "none"),
        ]
        if settings.is_production:
            base.append(_hdr("Strict-Transport-Security", "max-age=63072000; includeSubDomains"))
        self.base = base

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        is_docs = path.startswith(("/docs", "/redoc"))
        csp = DOCS_CSP if is_docs else CSP + ("; upgrade-insecure-requests" if settings.is_production else "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"server", b"x-powered-by")]
                headers.extend(self.base)
                headers.append(_hdr("Content-Security-Policy", csp))
                if path.startswith("/api/"):
                    headers.append(_hdr("Cache-Control", "no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _json_error(send: Send, status: int, detail: str, extra: Iterable[tuple[bytes, bytes]] = ()) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *extra]})
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Reject bodies over the limit (Content-Length check + streaming count for chunked uploads)."""

    def __init__(self, app: ASGIApp, default_limit: int, upload_limit: int, upload_prefixes: tuple[str, ...]):
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit
        self.upload_prefixes = upload_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope["path"].startswith(self.upload_prefixes) else self.default_limit
        cl = Headers(scope=scope).get("content-length")
        if cl is not None:
            try:
                if int(cl) > limit:
                    return await _json_error(send, 413, "Request body too large")
            except ValueError:
                return await _json_error(send, 400, "Invalid Content-Length")
        received = 0
        too_big = False

        async def limited_receive() -> Message:
            nonlocal received, too_big
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    too_big = True
                    raise _BodyTooLarge()
            return msg

        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if too_big:
                # the app turned our exception into some other error response: replace it with 413
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _json_error(send, 413, "Request body too large")
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not started:
                await _json_error(send, 413, "Request body too large")


class _BodyTooLarge(Exception):
    pass


class GlobalRateLimitMiddleware:
    """Per-IP budget for all /api traffic (login etc. have stricter per-route limits)."""

    def __init__(self, app: ASGIApp, per_minute: int):
        self.app = app
        self.per_minute = per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/api/"):
            ip = (scope.get("client") or ("unknown", 0))[0]
            retry = limiter.hit(f"global:{ip}", self.per_minute, 60)
            if retry is not None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                return await _json_error(send, 429, "Too many requests",
                                         [(b"retry-after", str(int(retry) + 1).encode())])
        await self.app(scope, receive, send)


class OriginCheckMiddleware:
    """Defence in depth vs CSRF: unsafe cross-origin requests from browsers are refused.

    Browsers always send `Origin` on cross-site POST/PUT/PATCH/DELETE; non-browser clients
    usually send none (they are covered by bearer auth instead).
    """

    def __init__(self, app: ASGIApp, allowed_origins: list[str]):
        self.app = app
        self.allowed = {o.lower() for o in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin and not origin_allowed(origin, headers.get("host", ""), self.allowed):
                return await _json_error(send, 403, "Cross-origin request blocked")
        await self.app(scope, receive, send)


def origin_allowed(origin: str, host: str, allowed: set[str]) -> bool:
    o = origin.lower().rstrip("/")
    if o in allowed:
        return True
    try:
        netloc = urlsplit(o).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc == host.lower()
