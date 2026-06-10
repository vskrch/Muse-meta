"""ASGI middleware for production HTTP safeguards."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    "cache-control": "no-store",
}


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before route handlers allocate them."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        """Initialize the middleware with an application and size limit."""
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle an ASGI request with request-body byte accounting."""
        if scope["type"] != "http" or self.max_body_size <= 0:
            await self.app(scope, receive, send)
            return

        if self._content_length_exceeds_limit(scope):
            await self._send_too_large(scope, receive, send)
            return

        bytes_seen = 0

        async def limited_receive() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if message["type"] == "http.request":
                bytes_seen += len(message.get("body", b""))
                if bytes_seen > self.max_body_size:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await self._send_too_large(scope, receive, send)

    def _content_length_exceeds_limit(self, scope: Scope) -> bool:
        """Return whether the Content-Length header is over the limit."""
        headers = dict(scope.get("headers", []))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is None:
            return False

        try:
            content_length = int(raw_content_length.decode("ascii"))
        except ValueError:
            return False
        return content_length > self.max_body_size

    async def _send_too_large(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = JSONResponse(
            {"detail": "Request body too large."},
            status_code=413,
        )
        await response(scope, receive, send)


class RequestBodyTooLargeError(Exception):
    """Raised when a streaming body exceeds the configured limit."""


class SecurityHeadersMiddleware:
    """Attach conservative security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the middleware with an ASGI application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle an ASGI request and attach response headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
