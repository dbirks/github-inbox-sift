"""Cross-origin and DNS-rebinding guards for a server that binds to loopback but is *reachable* by name.

Binding to 127.0.0.1 is not enough on its own: a page on any origin can make the browser issue requests to
loopback, and a malicious DNS record can point a public hostname at 127.0.0.1. So every request is checked:

* ``Host`` must be a loopback name (or ``testserver`` for the test client) — otherwise 421 *Misguided Request*.
* A present ``Origin`` must match that ``Host`` exactly — otherwise 403.
* State-changing methods must carry ``X-Inbox-Sift: 1``. A simple cross-origin form/fetch cannot set it, and a
  custom header forces a preflight the browser will not let through.

Responses all get nosniff/no-referrer plus a CSP that only allows same-origin scripts and styles.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# `testserver` is what Starlette's TestClient puts in Host.
_HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\]|testserver)(?::\d{1,5})?$", re.IGNORECASE)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REQUIRE_HEADER = "x-inbox-sift"

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://avatars.githubusercontent.com; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
}


def host_is_loopback(host: str | None) -> bool:
    return bool(host) and _HOST_RE.match(host.strip()) is not None


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    """True when `origin` is exactly the same host:port as `host` (empty port == empty port)."""
    if not origin:
        return True  # no Origin header: same-origin navigation, curl, or a non-browser client
    parts = urlsplit(origin)
    try:
        origin_port = str(parts.port) if parts.port is not None else ""
    except ValueError:
        return False  # malformed port
    if not parts.scheme or not parts.hostname:
        return False  # 'null', 'file:', garbage — never same-origin
    server_host, server_port = _split_authority(host or "")
    return parts.hostname.lower() == server_host and origin_port == server_port


def _split_authority(value: str) -> tuple[str, str]:
    """'127.0.0.1:8765' -> ('127.0.0.1','8765'); '[::1]:80' -> ('::1','80'); 'localhost' -> ('localhost','')."""
    v = value.strip().lower()
    if v.startswith("["):
        host, _, rest = v[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else ""
    host, sep, port = v.rpartition(":")
    if sep and port.isdigit():
        return host, port
    return v, ""


def _deny(status_code: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status_code)


class SecurityGuard:
    """Pure ASGI middleware: checks the request, then stamps the security headers onto whatever comes back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host = headers.get("host")
        method = str(scope.get("method", "GET")).upper()
        response: Any = None
        if not host_is_loopback(host):
            response = _deny(421, "bad_host")
        elif not origin_matches_host(headers.get("origin"), host):
            response = _deny(403, "bad_origin")
        elif method in _WRITE_METHODS and headers.get(REQUIRE_HEADER) != "1":
            response = _deny(403, "missing_header")

        if response is not None:
            await _send_with_security_headers(response, scope, receive, send)
            return

        await self.app(scope, receive, _wrap_send(send))


async def _send_with_security_headers(response: Any, scope: Scope, receive: Receive, send: Send) -> None:
    """Deny paths never reach the app, so they need the same headers stamped on manually."""
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    await response(scope, receive, send)


def _wrap_send(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            raw = MutableHeaders(raw=list(message.get("headers", [])))
            for key, value in SECURITY_HEADERS.items():
                if key.lower() not in raw:
                    raw[key] = value
            message = {**message, "headers": raw.raw}
        await send(message)

    return wrapped
