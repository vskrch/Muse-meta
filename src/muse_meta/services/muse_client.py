"""Client for communicating with Meta AI Muse Spark API.

Implements the reverse proxy pattern: translates OpenAI-compatible
requests into Muse Spark API calls and transforms responses back.
"""

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


class MuseClient:
    """HTTP client wrapper for Meta AI Muse Spark API.

    This client handles authentication, request transformation,
    and response mapping to maintain OpenAI API compatibility.

    Attributes:
        base_url: The base URL of the upstream Muse Spark API.
        api_key: Authentication key for the upstream API.
        timeout: Request timeout in seconds.
        _http_client: Async HTTP client instance.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the Muse client with application settings.

        Args:
            settings: Application configuration object.
        """
        self.base_url = settings.muse_base_url.rstrip("/")
        self.api_key = settings.muse_api_key
        self.timeout = settings.request_timeout
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return or lazily initialize the HTTP client.

        Returns:
            An async HTTP client configured with timeouts.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=self._default_headers(),
            )
        return self._http_client

    def _default_headers(self) -> dict[str, str]:
        """Build default HTTP headers for upstream requests.

        Returns:
            Dictionary of headers including authorization if key is set.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _transform_request(self, request: ChatCompletionRequest) -> dict:
        """Convert an OpenAI-style request into Muse Spark payload.

        Args:
            request: The OpenAI-compatible chat completion request.

        Returns:
            Dictionary payload for the Muse Spark API.
        """
        payload = {
            "model": request.model,
            "messages": [
                {"role": msg.role, "content": msg.content} for msg in request.messages
            ],
            "temperature": request.temperature,
            "top_p": request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stream:
            payload["stream"] = True
        return payload

    def _build_success_response(
        self,
        request: ChatCompletionRequest,
        content: str,
    ) -> ChatCompletionResponse:
        """Build an OpenAI-compatible completion response.

        Args:
            request: Original completion request.
            content: Generated text content from Muse.

        Returns:
            A fully populated ChatCompletionResponse.
        """
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=ChatMessage(role="assistant", content=content),
                    finish_reason="stop",
                ),
            ],
            usage=Usage(),
        )

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Send a non-streaming chat completion request to Muse Spark.

        Args:
            request: OpenAI-compatible chat completion request.

        Returns:
            OpenAI-compatible chat completion response.

        Raises:
            HTTPException: If the upstream request fails.
        """
        client = await self._get_client()
        payload = self._transform_request(request)

        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream Muse API error: {exc.response.status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Upstream Muse API unreachable: {exc!s}",
            ) from exc

        data = response.json()
        content = self._extract_content(data)
        return self._build_success_response(request, content)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionStreamResponse]:
        """Send a streaming chat completion request to Muse Spark.

        Args:
            request: OpenAI-compatible chat completion request.

        Yields:
            OpenAI-compatible streaming response chunks.

        Raises:
            HTTPException: If the upstream request fails.
        """
        client = await self._get_client()
        payload = self._transform_request(request)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    # TODO: Parse SSE lines and yield chunks.
                    # For now, yield a single content chunk placeholder.
                    yield ChatCompletionStreamResponse(
                        id=completion_id,
                        created=created,
                        model=request.model,
                        choices=[
                            StreamChoice(
                                index=0,
                                delta=DeltaMessage(content=line),
                            ),
                        ],
                    )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream Muse API error: {exc.response.status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Upstream Muse API unreachable: {exc!s}",
            ) from exc

        # Signal completion
        yield ChatCompletionStreamResponse(
            id=completion_id,
            created=created,
            model=request.model,
            choices=[
                StreamChoice(
                    index=0,
                    delta=DeltaMessage(),
                    finish_reason="stop",
                ),
            ],
        )

    def _extract_content(self, data: dict) -> str:
        """Extract generated text from a Muse Spark response.

        Args:
            data: Raw JSON response from Muse Spark.

        Returns:
            Extracted text content or empty string.
        """
        # Attempt common response shapes
        choices = data.get("choices")
        if choices:
            first = choices[0]
            if "message" in first:
                return first["message"].get("content", "")
            if "text" in first:
                return first["text"]
        if "response" in data:
            return data["response"]
        if "content" in data:
            return data["content"]
        if "output" in data:
            return data["output"]
        return ""

    async def close(self) -> None:
        """Close the underlying HTTP client connection pool."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


# Singleton instance for dependency injection
_muse_client_instance: MuseClient | None = None


def get_muse_client(settings: Settings) -> MuseClient:
    """Return the shared MuseClient instance.

    Args:
        settings: Application settings for configuring the client.

    Returns:
        A MuseClient instance.
    """
    global _muse_client_instance  # noqa: PLW0603
    if _muse_client_instance is None:
        _muse_client_instance = MuseClient(settings)
    return _muse_client_instance
