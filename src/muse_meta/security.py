"""Inbound API authentication and abuse controls."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from muse_meta.config import Settings, settings

_bearer_scheme = HTTPBearer(auto_error=False)
_MAX_RATE_LIMIT_IDENTITIES = 4096


def get_settings() -> Settings:
    """Return application settings for dependency injection."""
    return settings


def _unauthorized() -> HTTPException:
    """Build a consistent authentication failure response."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid bearer token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Require an OpenAI-style bearer token for protected API routes."""
    if not app_settings.require_api_key:
        return

    allowed_keys = app_settings.resolved_api_keys
    if not allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured.",
        )

    if credentials is None:
        raise _unauthorized()

    presented_key = credentials.credentials
    if not any(secrets.compare_digest(presented_key, key) for key in allowed_keys):
        raise _unauthorized()


class InMemoryRateLimiter:
    """Small per-process sliding-window limiter for edge-defense layering."""

    def __init__(self) -> None:
        """Initialize empty rate-limit buckets and synchronization state."""
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def retry_after_seconds(
        self,
        identity: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> int | None:
        """Record one hit and return retry-after seconds if the limit is exceeded."""
        if limit <= 0:
            return None

        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            events = self._events.setdefault(identity, deque())
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= limit:
                return max(1, int(window_seconds - (now - events[0])))

            events.append(now)
            self._prune_idle_identities(cutoff)
            return None

    def _prune_idle_identities(self, cutoff: float) -> None:
        """Bound limiter memory by dropping empty or oldest idle identities."""
        if len(self._events) <= _MAX_RATE_LIMIT_IDENTITIES:
            return

        stale_keys = [
            key
            for key, events in self._events.items()
            if not events or events[-1] <= cutoff
        ]
        for key in stale_keys:
            self._events.pop(key, None)


rate_limiter = InMemoryRateLimiter()


def _request_identity(request: Request) -> str:
    """Return a stable, non-secret identity for rate limiting."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"token:{digest}"

    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


async def enforce_rate_limit(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Apply an in-process request rate limit to protected API routes."""
    retry_after = await rate_limiter.retry_after_seconds(
        _request_identity(request),
        limit=app_settings.rate_limit_requests,
        window_seconds=app_settings.rate_limit_window_seconds,
    )
    if retry_after is None:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded.",
        headers={"Retry-After": str(retry_after)},
    )
