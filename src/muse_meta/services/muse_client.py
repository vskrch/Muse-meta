"""Async client for Meta AI using reverse-engineered GraphQL endpoints.

Based on the open-source meta-ai-api research, this client communicates
directly with Meta AI's backend to send chat messages and receive
responses, then translates them into OpenAI-compatible formats.

References:
    https://github.com/Strvm/meta-ai-api
"""

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncGenerator

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
META_AI_GRAPHQL = f"{META_AI_BASE}/api/graphql/"
META_AI_GRAPHQL_GRAPH = "https://graph.meta.ai/graphql?locale=user"

# Relay doc IDs — may change over time; these are current as of research
doc_ids = {
    "accept_tos": "7604648749596940",
    "send_message": "7783822248314888",
    "fetch_sources": "6946734308765963",
}


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


class MuseClient:
    """Async client that proxies OpenAI requests to Meta AI.

    Attributes:
        settings: Application configuration.
        _http: Shared async HTTP client.
        _access_token: Temporary access token for anonymous sessions.
        _cookies: Extracted cookies from meta.ai HTML page.
        _external_conversation_id: Current conversation UUID.
        _offline_threading_id: Last threading ID sent.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the client.

        Args:
            settings: App configuration including optional credentials.
        """
        self.settings = settings
        self._http: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._cookies: dict[str, str] = {}
        self._external_conversation_id: str | None = None
        self._offline_threading_id: str | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Return or create the shared HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout),
                headers={"user-agent": USER_AGENT},
            )
        return self._http

    async def _extract_cookies(self) -> dict[str, str]:
        """Scrape required cookies from the Meta AI homepage HTML.

        Returns:
            Dictionary of cookie names to values.
        """
        http = await self._get_http()
        resp = await http.get(META_AI_BASE)
        resp.raise_for_status()
        text = resp.text

        cookies = {
            "_js_datr": _extract_value(text, '_js_datr":{"value":"', '",'),
            "datr": _extract_value(text, 'datr":{"value":"', '",'),
            "lsd": _extract_value(text, '"LSD",[],{"token":"', '"}'),
            "fb_dtsg": _extract_value(text, 'DTSGInitData",[],{"token":"', '"'),
        }

        if self.settings.meta_username and self.settings.meta_password:
            # Authenticated mode: fb_dtsg + session cookie
            # For now, anonymous token flow is simpler and works without
            # brittle Facebook login scraping.
            pass
        else:
            cookies["abra_csrf"] = _extract_value(text, 'abra_csrf":{"value":"', '",')

        # Filter out empty values
        self._cookies = {k: v for k, v in cookies.items() if v}
        return self._cookies

    async def _get_access_token(self) -> str:
        """Obtain a temporary access token for anonymous use.

        Returns:
            A valid Meta AI access token string.
        """
        if self._access_token:
            return self._access_token

        if not self._cookies:
            await self._extract_cookies()

        http = await self._get_http()
        payload = {
            "lsd": self._cookies.get("lsd", ""),
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
                f"_js_datr={self._cookies.get('_js_datr', '')}; "
                f"abra_csrf={self._cookies.get('abra_csrf', '')}; "
                f"datr={self._cookies.get('datr', '')};"
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

        self._access_token = token
        # Brief pause so Meta can register cookies server-side
        await asyncio.sleep(1)
        return token

    def _build_send_payload(self, message: str) -> dict:
        """Construct the GraphQL payload for sending a message.

        Args:
            message: The user's text message.

        Returns:
            Dictionary ready for urlencode.
        """
        if not self._external_conversation_id:
            self._external_conversation_id = str(uuid.uuid4())

        self._offline_threading_id = _generate_offline_threading_id()

        variables = {
            "message": {"sensitive_string_value": message},
            "externalConversationId": self._external_conversation_id,
            "offlineThreadingId": self._offline_threading_id,
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
            "access_token": self._access_token,
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
        """Convert a Meta AI text response into OpenAI-compatible format.

        Args:
            request: Original OpenAI-style request.
            text: Plain text extracted from Meta AI.

        Returns:
            ChatCompletionResponse matching OpenAI schema.
        """
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

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Send a non-streaming chat completion to Meta AI.

        Args:
            request: OpenAI-compatible chat completion request.

        Returns:
            OpenAI-compatible chat completion response.
        """
        await self._get_access_token()
        http = await self._get_http()

        # Concatenate messages into a single prompt for Meta AI
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
                # Also update conversation/threading IDs
                chat_id = bot_msg.get("id")
                if chat_id and "_" in chat_id:
                    parts = chat_id.split("_")
                    self._external_conversation_id = parts[0]
                    self._offline_threading_id = parts[1]

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
        """Send a streaming chat completion to Meta AI.

        Args:
            request: OpenAI-compatible chat completion request.

        Yields:
            OpenAI-compatible streaming response chunks.
        """
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

                    # Track conversation IDs
                    chat_id = bot_msg.get("id")
                    if chat_id and "_" in chat_id:
                        parts = chat_id.split("_")
                        self._external_conversation_id = parts[0]
                        self._offline_threading_id = parts[1]

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

        # Final done chunk
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
        """Close the underlying HTTP client."""
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
