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
import logging
import random
import re
import time
import uuid
from collections.abc import AsyncGenerator, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

META_AI_BASE = "https://www.meta.ai"
META_AI_GRAPHQL = f"{META_AI_BASE}/api/graphql"

_DEFAULT_CHAT_DOC_IDS = [
    "ac0bad4b9787a393e160fb39f43404c1",
    "2f707e4a86f4b01adba97e1376cbdc14",
    "94e83840d1219339454cd5a6c97c1ece",
]

_HTTP_OK = status.HTTP_200_OK
_HTTP_FORBIDDEN = status.HTTP_403_FORBIDDEN
_HTTP_UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_LEGACY_COOKIE_FILE = Path(".meta_ai_cookies.json")
_COOKIE_FILE_MODE = 0o600
_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# Patterns to detect Meta AI challenge pages
_CHALLENGE_PATTERNS = (
    "executeChallenge",
    "__rd_verify",
    "rd_challenge",
    "challenge=3",
)


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
    delay = _BASE_DELAY * (2**attempt)
    jitter = random.uniform(0, delay * 0.5)
    return float(delay + jitter)


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

    def persist(self, cookie_file: Path) -> None:
        """Save cookie state to disk for reuse across restarts."""
        with contextlib.suppress(OSError):
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cookies": self.cookies,
                "created_at": self.created_at,
            }
            cookie_file.write_text(json.dumps(payload), encoding="utf-8")
            cookie_file.chmod(_COOKIE_FILE_MODE)

    @classmethod
    def load(cls, cookie_file: Path) -> MetaAISession | None:
        """Restore a session from disk if available and not stale."""
        for candidate in (cookie_file, _LEGACY_COOKIE_FILE):
            session = cls._load_candidate(candidate)
            if session is not None:
                return session
        return None

    @classmethod
    def _load_candidate(cls, cookie_file: Path) -> MetaAISession | None:
        """Load a single cookie file candidate."""
        if not cookie_file.exists():
            return None
        try:
            payload = json.loads(cookie_file.read_text(encoding="utf-8"))
            cookies = _coerce_cookie_dict(payload.get("cookies"))
            created_at = _coerce_float(payload.get("created_at"), default=0.0)
            session = cls(
                cookies=cookies,
                created_at=created_at,
            )
            if session.is_stale():
                return None
            return session
        except (OSError, json.JSONDecodeError):
            return None

    def clear(self, cookie_file: Path) -> None:
        """Reset all session state and remove persisted file."""
        self.access_token = ""
        self.cookies = {}
        self.external_conversation_id = ""
        self.created_at = 0.0
        with contextlib.suppress(OSError):
            cookie_file.unlink(missing_ok=True)

    def reset_auth(self) -> None:
        """Reset only auth-related state, keeping base cookies."""
        self.access_token = ""
        self.external_conversation_id = ""


def _is_challenge_page(html: str) -> bool:
    """Detect if an HTML response is a Meta AI challenge page."""
    return any(pattern in html for pattern in _CHALLENGE_PATTERNS)


def _coerce_cookie_dict(value: Any) -> dict[str, str]:
    """Convert decoded JSON cookie state into a string-only dictionary."""
    if not isinstance(value, dict):
        return {}
    return {str(key): str(cookie) for key, cookie in value.items() if cookie}


def _coerce_float(value: Any, *, default: float) -> float:
    """Convert decoded JSON numeric state into a float."""
    if isinstance(value, int | float):
        return float(value)
    return default


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
        self._cookie_file = settings.cookie_file_path
        self._session = MetaAISession.load(self._cookie_file) or MetaAISession()
        self._chat_doc_ids = self._resolve_chat_doc_ids()
        self._working_doc_id: str | None = None
        self._load_cookies_from_settings()

    def is_auth_configured(self) -> bool:
        """Return whether this client has any usable upstream auth material."""
        return bool(self._session.access_token or self._session.cookies)

    def health_snapshot(self) -> dict[str, object]:
        """Return non-secret client state for readiness diagnostics."""
        return {
            "auth_configured": self.is_auth_configured(),
            "cookie_count": len(self._session.cookies),
            "has_access_token": bool(self._session.access_token),
            "working_doc_id": self._working_doc_id,
        }

    def _load_cookies_from_settings(self) -> None:
        """Read cookie values from environment/settings."""
        if self.settings.meta_ai_datr:
            self._session.cookies["datr"] = self.settings.meta_ai_datr
        if self.settings.meta_ai_ecto_1_sess:
            self._session.cookies["ecto_1_sess"] = self.settings.meta_ai_ecto_1_sess
        if self.settings.meta_ai_abra_sess:
            self._session.cookies["abra_sess"] = self.settings.meta_ai_abra_sess
        if self.settings.meta_ai_access_token:
            self._session.access_token = self.settings.meta_ai_access_token

    def _resolve_chat_doc_ids(self) -> list[str]:
        """Resolve the list of chat doc_ids to try, with env overrides."""
        candidates = [
            self.settings.meta_ai_chat_doc_id,
            self.settings.meta_ai_chat_doc_id_alt,
            *_DEFAULT_CHAT_DOC_IDS,
        ]
        seen: set[str] = set()
        resolved: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                resolved.append(candidate)
                seen.add(candidate)
        return resolved

    def _ordered_doc_ids(self) -> list[str]:
        """Return doc_ids with the last working one first."""
        if self._working_doc_id and self._working_doc_id in self._chat_doc_ids:
            return [self._working_doc_id] + [
                d for d in self._chat_doc_ids if d != self._working_doc_id
            ]
        return list(self._chat_doc_ids)

    async def _get_http(self) -> httpx.AsyncClient:
        """Return or create the shared HTTP client."""
        if self._http is None:
            connect_timeout = min(10.0, self.settings.request_timeout)
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.settings.request_timeout,
                    connect=connect_timeout,
                ),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                ),
                follow_redirects=True,
            )
        return self._http

    def _get_cookie_header(self) -> str:
        """Build a Cookie header string from session cookies."""
        return "; ".join(f"{k}={v}" for k, v in self._session.cookies.items())

    def _sync_http_cookies(self, http: httpx.AsyncClient) -> None:
        """Copy cookies set by httpx responses into persisted session state."""
        for cookie in http.cookies.jar:
            if cookie.name and cookie.value is not None:
                self._session.cookies[cookie.name] = cookie.value

    def _build_request_headers(
        self,
        token: str,
        *,
        accept: str = "text/event-stream, application/json",
    ) -> dict[str, str]:
        """Build the full set of headers for a Meta AI request."""
        return {
            "cookie": self._get_cookie_header(),
            "authorization": f"OAuth {token}",
            "content-type": "application/json",
            "origin": META_AI_BASE,
            "referer": f"{META_AI_BASE}/",
            "accept": accept,
            "accept-language": "en-US,en;q=0.9",
            "user-agent": USER_AGENT,
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }

    async def _solve_challenge(
        self,
        http: httpx.AsyncClient,
        challenge_html: str,
    ) -> bool:
        """Solve Meta AI's client-side verification challenge.

        Meta AI returns a 403 page with a JS challenge that POSTs to a
        verify endpoint. We replicate that here to obtain the rd_challenge cookie.

        Args:
            http: The shared HTTP client.
            challenge_html: The HTML body of the 403 challenge response.

        Returns:
            True if the challenge was solved successfully.
        """
        # Try multiple challenge URL patterns
        patterns = [
            r"fetch\('([^']+)'",
            r'fetch\("([^"]+)"',
            r"fetch\(`([^`]+)`",
        ]
        challenge_path = None
        for pat in patterns:
            match = re.search(pat, challenge_html)
            if match:
                challenge_path = match.group(1)
                break

        if not challenge_path:
            logger.warning("No challenge URL found in response")
            return False

        challenge_url = f"{META_AI_BASE}{challenge_path}"
        logger.info("Solving challenge: %s", challenge_path)

        headers = {
            "cookie": self._get_cookie_header(),
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": USER_AGENT,
            "origin": META_AI_BASE,
            "referer": f"{META_AI_BASE}/",
        }

        try:
            resp = await http.post(challenge_url, headers=headers)
            if resp.status_code == _HTTP_OK:
                self._sync_http_cookies(http)
                logger.info("Challenge solved, rd_challenge cookie set")
                return True
            logger.warning("Challenge returned status %d", resp.status_code)
        except Exception:
            logger.exception("Failed to solve challenge")
        return False

    async def _handle_403_if_challenge(
        self,
        http: httpx.AsyncClient,
        resp: httpx.Response,
        request_headers: dict[str, str],
    ) -> httpx.Response:
        """Handle a 403 response by solving the challenge and retrying.

        If the response is a challenge page, solves it and retries the request.
        Returns the original response if it's not a challenge or if solving fails.
        """
        if resp.status_code != _HTTP_FORBIDDEN:
            return resp

        if not _is_challenge_page(resp.text):
            return resp

        logger.info("Meta AI returned challenge page (403), solving...")
        solved = await self._solve_challenge(http, resp.text)
        if not solved:
            return resp

        # Retry the original request with the challenge cookie now set
        try:
            return await http.get(
                META_AI_BASE,
                headers={
                    **request_headers,
                    "cookie": self._get_cookie_header(),
                    "accept": _HTML_ACCEPT,
                    "user-agent": USER_AGENT,
                },
            )
        except Exception:
            logger.exception("Failed to reload after challenge solve")
            return resp

    async def _extract_access_token(self) -> str:
        """Extract OAuth access token from meta.ai page HTML.

        Scans the page source for ecto1:... bearer tokens.
        Handles Meta AI's client-side challenge (403) automatically.

        Returns:
            The extracted access token.

        Raises:
            HTTPException: If no token is found.
        """
        http = await self._get_http()
        headers = {
            "cookie": self._get_cookie_header(),
            "accept": _HTML_ACCEPT,
            "accept-language": "en-US,en;q=0.5",
            "user-agent": USER_AGENT,
        }
        resp = await http.get(META_AI_BASE, headers=headers)

        # Handle 403 client challenge
        resp = await self._handle_403_if_challenge(http, resp, headers)

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

    @staticmethod
    def _extract_event_errors(event: dict) -> list[dict]:
        """Extract error objects from a GraphQL SSE event."""
        errors = []
        if not isinstance(event, dict):
            return errors
        data = event.get("data", {})
        if isinstance(data, dict):
            event_errors = data.get("errors", [])
            if isinstance(event_errors, list):
                errors.extend(event_errors)
        return errors

    @staticmethod
    def _is_done_event(event: dict) -> bool:
        """Check if a GraphQL event signals stream completion."""
        if not isinstance(event, dict):
            return False
        data = event.get("data", {})
        if isinstance(data, dict):
            stream = data.get("sendMessageStream", {})
            if isinstance(stream, dict):
                state = stream.get("streamingState")
                if isinstance(state, str) and state.upper() in ("DONE", "COMPLETED"):
                    return True
        return False

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

    def _log_gql_errors(self, doc_id: str, errors: list) -> None:
        """Log GraphQL validation errors with actionable detail."""
        for err in errors:
            msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
            logger.warning("GraphQL error [doc_id=%s]: %s", doc_id, msg)

    @staticmethod
    def _message_text(request: ChatCompletionRequest) -> str:
        """Flatten OpenAI chat messages into Meta AI's prompt text."""
        return "\n".join(f"{msg.role}: {msg.content}" for msg in request.messages)

    @staticmethod
    def _json_object_from_response(resp: httpx.Response) -> dict[str, Any] | None:
        """Return a JSON object response, or None for SSE/plain-text bodies."""
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _iter_sse_events(text: str) -> Iterator[dict[str, Any]]:
        """Yield decoded events from an SSE response body."""
        for raw_line in text.splitlines():
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
            if isinstance(event, dict):
                yield event

    def _iter_response_events(self, resp: httpx.Response) -> Iterator[dict[str, Any]]:
        """Yield GraphQL events from either JSON or SSE upstream responses."""
        payload = self._json_object_from_response(resp)
        if payload is not None:
            yield payload
            return
        yield from self._iter_sse_events(resp.text)

    @staticmethod
    def _snapshot_delta(last_snapshot: str, snapshot: str) -> str:
        """Return the incremental text difference between stream snapshots."""
        if not last_snapshot:
            return snapshot
        if snapshot.startswith(last_snapshot):
            return snapshot[len(last_snapshot) :]
        if snapshot == last_snapshot:
            return ""
        return snapshot

    def _collect_final_snapshot(self, events: Iterable[dict[str, Any]]) -> str:
        """Return the final assistant text snapshot from decoded events."""
        last_snapshot = ""
        yielded = False

        for event in events:
            conv_id = self._extract_conversation_id(event)
            if conv_id:
                self._session.external_conversation_id = conv_id

            snapshot = self._extract_message_from_event(event)
            if not snapshot:
                continue

            delta = self._snapshot_delta(last_snapshot, snapshot)
            last_snapshot = snapshot
            if delta.strip():
                yielded = True

        return last_snapshot.strip() if yielded else ""

    def _graphql_errors(self, resp: httpx.Response) -> list[dict[str, Any]]:
        """Return top-level GraphQL errors from a response, if present."""
        payload = self._json_object_from_response(resp)
        if not payload or payload.get("data"):
            return []

        errors = payload.get("errors", [])
        if not isinstance(errors, list):
            return []
        return [error for error in errors if isinstance(error, dict)]

    async def _post_chat_payload(
        self,
        http: httpx.AsyncClient,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> httpx.Response:
        """POST a chat payload, solving a challenge once when possible."""
        resp = await http.post(META_AI_GRAPHQL, headers=headers, json=payload)
        if resp.status_code != _HTTP_FORBIDDEN or not _is_challenge_page(resp.text):
            return resp

        solved = await self._solve_challenge(http, resp.text)
        if not solved:
            return resp

        retry_headers = {**headers, "cookie": self._get_cookie_header()}
        return await http.post(META_AI_GRAPHQL, headers=retry_headers, json=payload)

    def _raise_for_auth_or_status(self, resp: httpx.Response) -> None:
        """Raise clean HTTP errors for authentication and upstream failures."""
        if resp.status_code == _HTTP_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Meta AI authentication failed. Please refresh your cookies.",
            )
        resp.raise_for_status()

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
                    logger.info(
                        "Retry %d/%d after %.1fs: %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    self._session.reset_auth()
                    self._load_cookies_from_settings()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(f"Meta AI failed after {_MAX_RETRIES} retries: {last_error!s}"),
        ) from last_error

    async def _chat_completion_once(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Single attempt at non-streaming completion."""
        token = await self._get_access_token()
        http = await self._get_http()
        headers = self._build_request_headers(token)
        last_errors: list[dict] = []
        message_text = self._message_text(request)

        for doc_id in self._ordered_doc_ids():
            payload = self._build_chat_payload(message_text, doc_id)

            try:
                resp = await self._post_chat_payload(http, headers, payload)
                self._raise_for_auth_or_status(resp)
            except httpx.HTTPStatusError as exc:
                last_errors.append(
                    {"doc_id": doc_id, "status": exc.response.status_code}
                )
                continue
            except httpx.RequestError as exc:
                logger.warning("Request error with doc_id %s: %s", doc_id, exc)
                last_errors.append({"doc_id": doc_id, "error": str(exc)})
                continue

            gql_errors = self._graphql_errors(resp)
            if gql_errors:
                self._log_gql_errors(doc_id, gql_errors)
                last_errors.append(
                    {
                        "doc_id": doc_id,
                        "errors": [
                            error.get("message", "unknown") for error in gql_errors
                        ],
                    }
                )
                continue

            final_snapshot = self._collect_final_snapshot(
                self._iter_response_events(resp)
            )
            if final_snapshot:
                self._working_doc_id = doc_id
                return self._build_openai_response(request, final_snapshot)

            last_errors.append(
                {"doc_id": doc_id, "status": resp.status_code, "error": "empty"}
            )

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
                    logger.info(
                        "Stream retry %d/%d after %.1fs: %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    self._session.reset_auth()
                    self._load_cookies_from_settings()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Meta AI streaming failed after {_MAX_RETRIES} retries: {last_error!s}"
            ),
        ) from last_error

    async def _chat_completion_stream_once(  # noqa: PLR0912, PLR0915
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionStreamResponse]:
        """Single attempt at streaming completion."""
        token = await self._get_access_token()
        http = await self._get_http()

        headers = self._build_request_headers(token)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = request.model
        last_snapshot = ""
        yielded_any = False
        message_text = self._message_text(request)

        for doc_id in self._ordered_doc_ids():
            payload = self._build_chat_payload(message_text, doc_id)

            try:
                async with http.stream(
                    "POST",
                    META_AI_GRAPHQL,
                    headers=headers,
                    json=payload,
                ) as resp:
                    # Handle 403 challenge during streaming
                    if resp.status_code == _HTTP_FORBIDDEN:
                        body = await resp.aread()
                        body_text = body.decode(errors="replace")
                        if _is_challenge_page(body_text):
                            solved = await self._solve_challenge(http, body_text)
                            if solved:
                                logger.info(
                                    "Solved challenge during streaming; "
                                    "retrying with doc_id %s",
                                    doc_id,
                                )
                                continue
                        logger.warning("Streaming doc_id %s returned 403", doc_id)
                        continue

                    self._raise_for_auth_or_status(resp)

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

                        delta = self._snapshot_delta(last_snapshot, snapshot)
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
                if exc.response.status_code == _HTTP_UNAUTHORIZED:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Meta AI authentication failed. "
                        "Please refresh your cookies.",
                    ) from exc
                logger.warning("Stream HTTP error with doc_id %s: %s", doc_id, exc)
                continue
            except httpx.RequestError as exc:
                logger.warning("Stream request error with doc_id %s: %s", doc_id, exc)
                continue

            if yielded_any:
                self._working_doc_id = doc_id
                break

        if not yielded_any:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Meta AI returned an empty or unexpected stream.",
            )

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
        self._session.persist(self._cookie_file)
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
    if _muse_client_instance is None or _muse_client_instance.settings is not settings:
        _muse_client_instance = MuseClient(settings)
    return _muse_client_instance
