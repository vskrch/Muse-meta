"""Async client for Meta AI using modern OAuth + cookie authentication.

Based on the metaai-api research, this client communicates directly
with Meta AI's GraphQL backend using an OAuth access token and
cookies from the user's browser session.

Authentication:
    Set META_AI_DATR and META_AI_ECTO_1_SESS in your .env file,
    or let the client attempt anonymous extraction.

References:
    https://github.com/mir-ashiq/metaai-api
    https://github.com/Strvm/meta-ai-api
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

META_AI_BASE = "https://www.meta.ai"
META_AI_GRAPHQL = f"{META_AI_BASE}/api/graphql"

_CHAT_DOC_IDS = [
    "ac0bad4b9787a393e160fb39f43404c1",
    "2f707e4a86f4b01adba97e1376cbdc14",
]

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


def _jittered_delay(attempt: int) -> float:
    """Compute exponential backoff delay with jitter."""
    delay = _BASE_DELAY * (2 ** attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter


@dataclass
class MetaAISession:
    """Encapsulates auth state for a single Meta AI session."""

    access_token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    external_conversation_id: str = ""
    created_at: float = field(default_factory=time.time)

    def is_stale(self, max_age_seconds: float = 1800.0) -> bool:
        """Check if the session is older than the allowed age."""
        return (time.time() - self.created_at) > max_age_seconds

    def persist(self) -> None:
        """Save cookie state to disk for reuse across restarts."""
        with contextlib.suppress(OSError):
            payload = {
                "cookies": self.cookies,
                "created_at": self.created_at,
            }
            _COOKIE_FILE.write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls) -> MetaAISession | None:
        """Restore a session from disk if available and not stale."""
        if not _COOKIE_FILE.exists():
            return None
        try:
            payload = json.loads(_COOKIE_FILE.read_text(encoding="utf-8"))
            session = cls(
                cookies=payload.get("cookies", {}),
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
        self.created_at = 0.0
        with contextlib.suppress(OSError):
            _COOKIE_FILE.unlink(missing_ok=True)


class MuseClient:
    """Async client that proxies OpenAI requests to Meta AI.

    Uses OAuth access_token + cookie auth as per modern Meta AI API.

    To authenticate, set these environment variables in your .env:
        META_AI_DATR=your_datr_cookie
        META_AI_ECTO_1_SESS=your_ecto_1_sess_cookie
        META_AI_ACCESS_TOKEN=your_oauth_token (optional)

    Or pass cookies when instantiating the client.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the client.

        Args:
            settings: App configuration with optional cookie values.
        """
        self.settings = settings
        self._http: httpx.AsyncClient | None = None
        self._session = MetaAISession.load() or MetaAISession()
        self._load_cookies_from_settings()

    def _load_cookies_from_settings(self) -> None:
        """Read cookie values from environment/settings."""
        if self.settings.meta_ai_datr:
            self._session.cookies["datr"] = self.settings.meta_ai_datr
        if self.settings.meta_ai_ecto_1_sess:
            self._session.cookies["ecto_1_sess"] = (
                self.settings.meta_ai_ecto_1_sess
            )
        if self.settings.meta_ai_abra_sess:
            self._session.cookies["abra_sess"] = self.settings.meta_ai_abra_sess
        if self.settings.meta_ai_access_token:
            self._session.access_token = self.settings.meta_ai_access_token

    async def _get_http(self) -> httpx.AsyncClient:
        """Return or create the shared HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout),
                headers={"user-agent": USER_AGENT},
            )
        return self._http

    def _get_cookie_header(self) -> str:
        """Build a Cookie header string from session cookies."""
        return "; ".join(f"{k}={v}" for k, v in self._session.cookies.items())

    async def _extract_access_token(self) -> str:
        """Extract OAuth access token from meta.ai page HTML.

        Scans the page source for ecto1:... bearer tokens.

        Returns:
            The extracted access token.

        Raises:
            HTTPException: If no token is found.
        """
        http = await self._get_http()
        headers = {
            "cookie": self._get_cookie_header(),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
        resp = await http.get(META_AI_BASE, headers=headers)
        resp.raise_for_status()

        # Search for ecto1 token pattern
        match = re.search(r"ecto1:[a-zA-Z0-9_-]+", resp.text)
        if match:
            token = match.group(0)
            self._session.access_token = token
            return token

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to extract Meta AI OAuth token. "
            "Please set META_AI_ACCESS_TOKEN in your .env file.",
        )

    async def _get_access_token(self) -> str:
        """Return existing or freshly extracted access token."""
        if self._session.access_token:
            return self._session.access_token
        return await self._extract_access_token()

    def _build_chat_payload(self, message: str, doc_id: str) -> dict:
        """Construct the GraphQL JSON payload for chat.

        Args:
            message: User's text message.
            doc_id: GraphQL persisted query doc_id.

        Returns:
            Dictionary payload for the POST request.
        """
        if not self._session.external_conversation_id:
            self._session.external_conversation_id = str(uuid.uuid4())

        return {
            "doc_id": doc_id,
            "variables": {
                "conversationId": self._session.external_conversation_id,
                "content": message,
                "userMessageId": str(uuid.uuid4()),
                "assistantMessageId": str(uuid.uuid4()),
                "userUniqueMessageId": str(uuid.uuid4().int)[:19],
                "turnId": str(uuid.uuid4()),
                "mode": "create",
                "isNewConversation": True,
                "clientTimezone": "America/New_York",
                "entryPoint": "KADABRA__UNKNOWN",
                "promptSessionId": str(uuid.uuid4()),
                "userAgent": USER_AGENT,
                "currentBranchPath": "0",
                "promptEditType": "new_message",
                "userLocale": "en-US",
                "attachments": None,
                "attachmentsV2": None,
                "mentions": None,
                "rewriteOptions": None,
                "imagineOperationRequest": None,
            },
        }

    @staticmethod
    def _extract_text_from_dict(obj: dict) -> str | None:
        """Try common text fields in a message dictionary."""
        for key in ("content", "text"):
            val = obj.get(key)
            if isinstance(val, str):
                return val
        composed = obj.get("composed_text", {})
        if isinstance(composed, dict):
            items = composed.get("content", [])
            if isinstance(items, list):
                return "\n".join(
                    i.get("text", "") for i in items if isinstance(i, dict)
                )
        return None

    @classmethod
    def _extract_message_from_event(cls, event: dict) -> str:
        """Pull assistant text from a Meta AI SSE event."""
        if not isinstance(event, dict):
            return ""
        data = event.get("data", {})
        if not isinstance(data, dict):
            return ""

        # Check sendMessageStream -> message
        stream = data.get("sendMessageStream", {})
        if isinstance(stream, dict):
            msg = stream.get("message", {})
            if isinstance(msg, dict):
                text = cls._extract_text_from_dict(msg)
                if text is not None:
                    return text

        # Check message directly
        msg = data.get("message", {})
        if isinstance(msg, dict):
            text = cls._extract_text_from_dict(msg)
            if text is not None:
                return text

        # Check node -> bot_response_message
        node = data.get("node", {})
        if isinstance(node, dict):
            bot = node.get("bot_response_message", {})
            if isinstance(bot, dict):
                text = cls._extract_text_from_dict(bot)
                if text is not None:
                    return text

        return ""

    @staticmethod
    def _extract_conversation_id(event: dict) -> str | None:
        """Extract conversation ID from an SSE event."""
        if not isinstance(event, dict):
            return None
        data = event.get("data", {})
        if not isinstance(data, dict):
            return None
        stream = data.get("sendMessageStream", {})
        if isinstance(stream, dict):
            cid = stream.get("conversationId")
            if isinstance(cid, str) and cid:
                return cid
        return None

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
                raise
            except Exception as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _jittered_delay(attempt)
                    await asyncio.sleep(delay)
                    self._session.clear()
                    self._load_cookies_from_settings()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Meta AI failed after {_MAX_RETRIES} retries: "
                f"{last_error!s}"
            ),
        ) from last_error

    async def _chat_completion_once(  # noqa: PLR0912
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Single attempt at non-streaming completion."""
        token = await self._get_access_token()
        http = await self._get_http()

        message_text = "\n".join(
            f"{msg.role}: {msg.content}" for msg in request.messages
        )

        headers = {
            "cookie": self._get_cookie_header(),
            "authorization": f"OAuth {token}",
            "content-type": "application/json",
            "origin": META_AI_BASE,
            "referer": f"{META_AI_BASE}/",
            "accept": "text/event-stream, application/json",
            "accept-language": "en-US,en;q=0.9",
        }

        last_snapshot = ""
        yielded = False
        last_errors: list[dict] = []

        for doc_id in _CHAT_DOC_IDS:
            payload = self._build_chat_payload(message_text, doc_id)

            try:
                resp = await http.post(
                    META_AI_GRAPHQL,
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == status.HTTP_401_UNAUTHORIZED:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Meta AI authentication failed. "
                        "Please refresh your cookies.",
                    ) from exc
                last_errors.append({
                    "doc_id": doc_id,
                    "status": exc.response.status_code,
                })
                continue

            # Parse SSE response
            for raw_line in resp.text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("event:"):
                    continue

                data = line[5:].strip() if line.startswith("data:") else line
                if data in ("[DONE]", "null", ""):
                    continue

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # Track conversation ID
                conv_id = self._extract_conversation_id(event)
                if conv_id:
                    self._session.external_conversation_id = conv_id

                # Extract text
                snapshot = self._extract_message_from_event(event)
                if not snapshot:
                    continue

                if not last_snapshot:
                    delta = snapshot
                elif snapshot.startswith(last_snapshot):
                    delta = snapshot[len(last_snapshot):]
                elif snapshot == last_snapshot:
                    delta = ""
                else:
                    delta = snapshot

                last_snapshot = snapshot
                if delta.strip():
                    yielded = True

            if yielded:
                return self._build_openai_response(request, last_snapshot.strip())

        if last_errors:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Meta AI GraphQL errors: {last_errors}",
            )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Meta AI returned an empty or unexpected response.",
        )

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
                    self._load_cookies_from_settings()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Meta AI streaming failed after {_MAX_RETRIES} "
                f"retries: {last_error!s}"
            ),
        ) from last_error

    async def _chat_completion_stream_once(  # noqa: PLR0912
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionStreamResponse]:
        """Single attempt at streaming completion."""
        token = await self._get_access_token()
        http = await self._get_http()

        message_text = "\n".join(
            f"{msg.role}: {msg.content}" for msg in request.messages
        )

        headers = {
            "cookie": self._get_cookie_header(),
            "authorization": f"OAuth {token}",
            "content-type": "application/json",
            "origin": META_AI_BASE,
            "referer": f"{META_AI_BASE}/",
            "accept": "text/event-stream, application/json",
            "accept-language": "en-US,en;q=0.9",
        }

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = request.model
        last_snapshot = ""
        yielded_any = False

        for doc_id in _CHAT_DOC_IDS:
            payload = self._build_chat_payload(message_text, doc_id)

            try:
                async with http.stream(
                    "POST",
                    META_AI_GRAPHQL,
                    headers=headers,
                    json=payload,
                ) as resp:
                    resp.raise_for_status()

                    async for raw_line in resp.aiter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith("event:"):
                            continue

                        data = line[5:].strip() if line.startswith("data:") else line
                        if data in ("[DONE]", "null", ""):
                            continue

                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        conv_id = self._extract_conversation_id(event)
                        if conv_id:
                            self._session.external_conversation_id = conv_id

                        snapshot = self._extract_message_from_event(event)
                        if not snapshot:
                            continue

                        if not last_snapshot:
                            delta = snapshot
                        elif snapshot.startswith(last_snapshot):
                            delta = snapshot[len(last_snapshot):]
                        elif snapshot == last_snapshot:
                            delta = ""
                        else:
                            delta = snapshot

                        last_snapshot = snapshot
                        if delta.strip():
                            yielded_any = True
                            yield ChatCompletionStreamResponse(
                                id=completion_id,
                                created=created,
                                model=model,
                                choices=[
                                    StreamChoice(
                                        index=0,
                                        delta=DeltaMessage(content=delta),
                                    ),
                                ],
                            )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == status.HTTP_401_UNAUTHORIZED:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Meta AI authentication failed. "
                        "Please refresh your cookies.",
                    ) from exc
                continue

            if yielded_any:
                break

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
