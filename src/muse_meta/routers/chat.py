"""OpenAI-compatible chat completion endpoints.

This router exposes the standard /v1/chat/completions endpoint
and proxies requests to Meta AI Muse Spark.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from muse_meta.config import Settings, settings
from muse_meta.models.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from muse_meta.services.muse_client import MuseClient, get_muse_client

router = APIRouter(prefix="/v1", tags=["chat"])


def _get_settings() -> Settings:
    """Return application settings for dependency injection.

    Returns:
        The global settings instance.
    """
    return settings


def _get_client(dep_settings: Settings = Depends(_get_settings)) -> MuseClient:
    """Return a configured MuseClient for dependency injection.

    Args:
        dep_settings: Injected application settings.

    Returns:
        A MuseClient ready to communicate with the upstream API.
    """
    return get_muse_client(dep_settings)


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    status_code=status.HTTP_200_OK,
    summary="Create chat completion",
    description=(
        "Proxies an OpenAI-compatible chat completion request to "
        "Meta AI Muse Spark and returns the response."
    ),
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    client: MuseClient = Depends(_get_client),
) -> ChatCompletionResponse | StreamingResponse:
    """Handle chat completion requests.

    Args:
        request: OpenAI-compatible chat completion payload.
        client: Muse Spark API client.

    Returns:
        Either a full completion response or a server-sent event stream.

    Raises:
        HTTPException: If the request is invalid or upstream fails.
    """
    if not request.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messages list cannot be empty.",
        )

    if request.stream:
        return StreamingResponse(
            _stream_response(client, request),
            media_type="text/event-stream",
        )

    return await client.chat_completion(request)


async def _stream_response(
    client: MuseClient,
    request: ChatCompletionRequest,
) -> StreamingResponse:
    """Yield server-sent events for streaming completions.

    Args:
        client: Muse Spark API client.
        request: Original chat completion request.

    Yields:
        SSE-formatted JSON strings compatible with OpenAI clients.
    """
    async for chunk in client.chat_completion_stream(request):
        yield f"data: {chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"
