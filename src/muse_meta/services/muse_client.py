"""Async client for Meta AI using reverse-engineered GraphQL endpoints.

Fail-safe design with multiple auth strategies, cookie persistence,
automatic retry, and stale-session recovery.

References:
    https://github.com/Strvm/meta-ai-api
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import HTTPException, status

from muse_meta.config import Settings
from muse_meta.models.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamResponse,
    ChatMessage,
    Choice,
    DeltaMessage,
    StreamChoice,
    Usage,
)

_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

META_AI_BASE = "https://www.meta.ai"
META_AI_GRAPHQL = f"{META_AI_BASE}/api/graphql/"
META_AI_GRAPHQL_GRAPH = "https://graph.meta.ai/graphql?locale=user"

# Relay doc IDs — may change over time; these are current as of research
doc_ids = {
    "accept_tos": "7604648749596940",
    "send_message": "7783822248314888",
    "fetch_sources": "6946734308765963",
}

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_COOKIE_FILE = Path(".meta_ai_cookies.json")


def _generate_offline_threading_id() -> str:
    """Generate a valid offline threading ID for Meta AI requests."""
    max_int = (1 << 64) - 1
    mask22 = (1 << 22) - 1
    timestamp = int(time.time() * 1000)
    random_bits = random.getrandbits(64)
    shifted = timestamp << 22
    masked = random_bits & mask22
    return str((shifted | masked) & max_int)


def _extract_value(text: str, start_str: str, end_str: str) -> str:
    """Extract a substring between two markers."""
    start = text.find(start_str) + len(start_str)
    if start < len(start_str):
        return ""
    end = text.find(end_str, start)
    if end == -1:
        return ""
    return text[start:end]


def _format_bot_response(json_line: dict) -> str:
    """Extract plain text from a Meta AI response JSON line."""
    content_list = (
        json_line.get("data", {})
        .get("node", {})
        .get("bot_response_message", {})
        .get("composed_text", {})
        .get("content", [])
    )
    return "".join(item.get("text", "") for item in content_list)


def _jittered_delay(attempt: int) -> float:
    """Compute exponential backoff delay with jitter.

    Args:
        attempt: Zero-based retry attempt number.

    Returns:
        Delay in seconds to wait before next retry.
    """
    delay = _BASE_DELAY * (2**attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter


@dataclass
class MetaAISession:
    """Encapsulates auth state for a single Meta AI session.

    Attributes:
        access_token: Temporary anonymous token.
        cookies: Dictionary of cookie key-value pairs.
        external_conversation_id: UUID for the current conversation.
        offline_threading_id: Last generated threading ID.
        is_authenticated: Whether this session uses logged-in cookies.
        created_at: Unix timestamp when the session was established.
    """

    access_token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    external_conversation_id: str = ""
    offline_threading_id: str = ""
    is_authenticated: bool = False
    created_at: float = field(default_factory=time.time)

    def is_stale(self, max_age_seconds: float = 1800.0) -> bool:
        """Check if the session is older than the allowed age.

        Args:
            max_age_seconds: Maximum age before considering stale.

        Returns:
            True if the session should be refreshed.
        """
        return (time.time() - self.created_at) > max_age_seconds

    def persist(self) -> None:
        """Save cookie state to disk for reuse across restarts."""
        try:
            payload = {
                "cookies": self.cookies,
                "is_authenticated": self.is_authenticated,
                "created_at": self.created_at,
            }
            _COOKIE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # Non-critical; continue without persistence

    @classmethod
    def load(cls) -> MetaAISession | None:
        """Restore a session from disk if available and not stale.

        Returns:
            Restored session, or None if no valid saved session exists.
        """
        if not _COOKIE_FILE.exists():
            return None
        try:
            payload = json.loads(_COOKIE_FILE.read_text(encoding="utf-8"))
            session = cls(
                cookies=payload.get("cookies", {}),
                is_authenticated=payload.get("is_authenticated", False),
                created_at=payload.get("created_at", 0.0),
            )
            if session.is_stale():
                return None
            return session
        except (OSError, json.JSONDecodeError):
            return None

    def clear(self) -> None:
        """Reset all session state and remove persisted file."""
        self.access_token = ""
        self.cookies = {}
        self.external_conversation_id = ""
        self.offline_threading_id = ""
        self.is_authenticated = False
        self.created_at = 0.0
        with contextlib.suppress(OSError):
            _COOKIE_FILE.unlink(missing_ok=True)


class MuseClient:
    """Async client that proxies OpenAI requests to Meta AI.

    Implements a fail-safe auth chain:
        1. Try saved session cookies from disk.
        2. Try Playwright browser extraction.
        3. Fall back to direct HTTP (likely to 403).

    Handles stale sessions by auto-refreshing cookies and
    retrying requests with exponential backoff.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the client.

        Args:
            settings: App configuration including optional credentials.
        """
        self.settings = settings
        self._http: httpx.AsyncClient | None = None
        self._session = MetaAISession.load() or MetaAISession()

    async def _get_http(self) -> httpx.AsyncClient:
        """Return or create the shared HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout),
                headers={"user-agent": USER_AGENT},
            )
        return self._http

    # ------------------------------------------------------------------
    # Cookie extraction strategies
    # ------------------------------------------------------------------

    async def _extract_cookies(self) -> dict[str, str]:
        """Extract cookies using the best available strategy.

        Returns:
            Dictionary of cookie names to values.
        """
        # Strategy 1: Playwright browser (most robust against bot detection)
        if _PLAYWRIGHT_AVAILABLE:
            try:
                cookies = await self._extract_cookies_via_browser()
                if cookies:
                    self._session.cookies = cookies
                    self._session.persist()
                    return cookies
            except Exception:
                pass  # Fallback to next strategy

        # Strategy 2: Direct HTTP (fastest but often blocked)
        try:
            cookies = await self._extract_cookies_via_http()
            if cookies:
                self._session.cookies = cookies
                self._session.persist()
                return cookies
        except Exception:
            pass

        # Nothing worked
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Unable to extract Meta AI cookies. "
                "Install Playwright (pip install playwright) for best results."
            ),
        )

    async def _extract_cookies_via_http(self) -> dict[str, str]:
        """Direct HTTP cookie extraction (may 403 on bot detection)."""
        http = await self._get_http()
        resp = await http.get(META_AI_BASE)
        resp.raise_for_status()
        return self._parse_cookies_from_html(resp.text)

    async def _extract_cookies_via_browser(self) -> dict[str, str]:
        """Use Playwright to visit meta.ai and extract cookies."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=USER_AGENT,
            )
            page = await context.new_page()
            try:
                resp = await page.goto(
                    META_AI_BASE,
                    wait_until="networkidle",
                    timeout=30000,
                )
                if resp is None or resp.status >= status.HTTP_BAD_REQUEST:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Meta AI blocked the browser request.",
                    )
                text = await page.content()
                cookies = self._parse_cookies_from_html(text)

                # Also harvest HTTP-only cookies from browser storage
                storage = await context.cookies()
                interesting = ("datr", "_js_datr", "abra_csrf")
                for cookie in storage:
                    name = cookie.get("name", "")
                    if name in interesting and name not in cookies:
                        cookies[name] = cookie.get("value", "")

                return cookies
            finally:
                await browser.close()

    def _parse_cookies_from_html(self, text: str) -> dict[str, str]:
        """Parse cookie values embedded in Meta AI HTML source.

        Args:
            text: Raw HTML page source.

        Returns:
            Dictionary of cookie key-value pairs.
        """
        cookies = {
            "_js_datr": _extract_value(text, '_js_datr":{"value":"', '",'),
            "datr": _extract_value(text, 'datr":{"value":"', '",'),
            "lsd": _extract_value(text, '"LSD",[],{"token":"', '"}'),
            "fb_dtsg": _extract_value(text, 'DTSGInitData",[],{"token":"', '"'),
        }

        if not (self.settings.meta_username and self.settings.meta_password):
            cookies["abra_csrf"] = _extract_value(text, 'abra_csrf":{"value":"', '",')

        return {k: v for k, v in cookies.items() if v}

    # ------------------------------------------------------------------
    # Access token
    # ------------------------------------------------------------------

    async def _get_access_token(self) -> str:
        """Obtain a temporary access token for anonymous use.

        Returns:
            A valid Meta AI access token string.
        """
        if self._session.access_token:
            return self._session.access_token

        if not self._session.cookies:
            await self._extract_cookies()

        http = await self._get_http()
        payload = {
            "lsd": self._session.cookies.get("lsd", ""),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "useAbraAcceptTOSForTempUserMutation",
            "variables": json.dumps(
                {
                    "dob": "1999-01-01",
                    "icebreaker_type": "TEXT",
                    "__relay_internal__pv__WebPixelRatiorelayprovider": 1,
                }
            ),
            "doc_id": doc_ids["accept_tos"],
        }
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "cookie": (
                f"_js_datr={self._session.cookies.get('_js_datr', '')}; "
                f"abra_csrf={self._session.cookies.get('abra_csrf', '')}; "
                f"datr={self._session.cookies.get('datr', '')};"
            ),
            "sec-fetch-site": "same-origin",
            "x-fb-friendly-name": "useAbraAcceptTOSForTempUserMutation",
        }

        resp = await http.post(
            META_AI_GRAPHQL,
            data=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        try:
            token = data["data"]["xab_abra_accept_terms_of_service"][
                "new_temp_user_auth"
            ]["access_token"]
        except (KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to obtain Meta AI access token. Region may be blocked.",
            ) from exc

        self._session.access_token = token
        self._session.persist()
        await asyncio.sleep(1)  # Let Meta register cookies server-side
        return token

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _build_send_payload(self, message: str) -> dict:
        """Construct the GraphQL payload for sending a message.

        Args:
            message: The user's text message.

        Returns:
            Dictionary ready for urlencode.
        """
        if not self._session.external_conversation_id:
            self._session.external_conversation_id = str(uuid.uuid4())

        self._session.offline_threading_id = _generate_offline_threading_id()

        variables = {
            "message": {"sensitive_string_value": message},
            "externalConversationId": self._session.external_conversation_id,
            "offlineThreadingId": self._session.offline_threading_id,
            "suggestedPromptIndex": None,
            "flashVideoRecapInput": {"images": []},
            "flashPreviewInput": None,
            "promptPrefix": None,
            "entrypoint": "ABRA__CHAT__TEXT",
            "icebreaker_type": "TEXT",
            "__relay_internal__pv__AbraDebugDevOnlyrelayprovider": False,
            "__relay_internal__pv__WebPixelRatiorelayprovider": 1,
        }

        payload = {
            "access_token": self._session.access_token,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "useAbraSendMessageMutation",
            "variables": json.dumps(variables),
            "server_timestamps": "true",
            "doc_id": doc_ids["send_message"],
        }
        return payload

    def _build_openai_response(
        self,
        request: ChatCompletionRequest,
        text: str,
    ) -> ChatCompletionResponse:
        """Convert Meta AI text into OpenAI-compatible format."""
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=ChatMessage(role="assistant", content=text),
                    finish_reason="stop",
                ),
            ],
            usage=Usage(),
        )

    # ------------------------------------------------------------------
    # Core chat methods with retry
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Send a non-streaming chat completion with automatic retry.

        Args:
            request: OpenAI-compatible chat completion request.

        Returns:
            OpenAI-compatible chat completion response.
        """
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await self._chat_completion_once(request)
            except HTTPException:
                raise  # Don't retry client-side validation errors
            except Exception as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _jittered_delay(attempt)
                    await asyncio.sleep(delay)
                    # Wipe potentially stale state and try again
                    self._session.clear()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Meta AI failed after {_MAX_RETRIES} retries: {last_error!s}",
        ) from last_error

    async def _chat_completion_once(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Single attempt at non-streaming completion."""
        await self._get_access_token()
        http = await self._get_http()

        message_text = "\n".join(
            f"{msg.role}: {msg.content}" for msg in request.messages
        )

        payload = self._build_send_payload(message_text)
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "x-fb-friendly-name": "useAbraSendMessageMutation",
        }

        resp = await http.post(
            META_AI_GRAPHQL_GRAPH,
            data=payload,
            headers=headers,
        )
        resp.raise_for_status()

        last_line = None
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                json_line = json.loads(line)
            except json.JSONDecodeError:
                continue
            bot_msg = (
                json_line.get("data", {})
                .get("node", {})
                .get("bot_response_message", {})
            )
            streaming_state = bot_msg.get("streaming_state")
            if streaming_state == "OVERALL_DONE":
                last_line = json_line
                chat_id = bot_msg.get("id")
                if chat_id and "_" in chat_id:
                    parts = chat_id.split("_")
                    self._session.external_conversation_id = parts[0]
                    self._session.offline_threading_id = parts[1]

        if last_line is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Meta AI returned an empty or unexpected response.",
            )

        text = _format_bot_response(last_line)
        return self._build_openai_response(request, text)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionStreamResponse]:
        """Send a streaming chat completion with automatic retry.

        Args:
            request: OpenAI-compatible chat completion request.

        Yields:
            OpenAI-compatible streaming response chunks.
        """
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async for chunk in self._chat_completion_stream_once(request):
                    yield chunk
                return
            except HTTPException:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _jittered_delay(attempt)
                    await asyncio.sleep(delay)
                    self._session.clear()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Meta AI streaming failed after {_MAX_RETRIES} retries: {last_error!s}"
            ),
        ) from last_error

    async def _chat_completion_stream_once(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionStreamResponse]:
        """Single attempt at streaming completion."""
        await self._get_access_token()
        http = await self._get_http()

        message_text = "\n".join(
            f"{msg.role}: {msg.content}" for msg in request.messages
        )

        payload = self._build_send_payload(message_text)
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "x-fb-friendly-name": "useAbraSendMessageMutation",
        }

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = request.model

        try:
            async with http.stream(
                "POST",
                META_AI_GRAPHQL_GRAPH,
                data=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    if not raw_line.strip():
                        continue
                    try:
                        json_line = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue

                    bot_msg = (
                        json_line.get("data", {})
                        .get("node", {})
                        .get("bot_response_message", {})
                    )
                    text = _format_bot_response(json_line)
                    if not text:
                        continue

                    yield ChatCompletionStreamResponse(
                        id=completion_id,
                        created=created,
                        model=model,
                        choices=[
                            StreamChoice(
                                index=0,
                                delta=DeltaMessage(content=text),
                            ),
                        ],
                    )

                    chat_id = bot_msg.get("id")
                    if chat_id and "_" in chat_id:
                        parts = chat_id.split("_")
                        self._session.external_conversation_id = parts[0]
                        self._session.offline_threading_id = parts[1]
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Meta AI upstream error: {exc.response.status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Meta AI unreachable: {exc!s}",
            ) from exc

        yield ChatCompletionStreamResponse(
            id=completion_id,
            created=created,
            model=model,
            choices=[
                StreamChoice(
                    index=0,
                    delta=DeltaMessage(),
                    finish_reason="stop",
                ),
            ],
        )

    async def close(self) -> None:
        """Close the underlying HTTP client and persist state."""
        self._session.persist()
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# Singleton instance for dependency injection
_muse_client_instance: MuseClient | None = None


def get_muse_client(settings: Settings) -> MuseClient:
    """Return the shared MuseClient instance.

    Args:
        settings: Application settings.

    Returns:
        Configured MuseClient.
    """
    global _muse_client_instance  # noqa: PLW0603
    if _muse_client_instance is None:
        _muse_client_instance = MuseClient(settings)
    return _muse_client_instance
