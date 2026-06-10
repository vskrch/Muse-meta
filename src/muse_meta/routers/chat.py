"""OpenAI-compatible chat completion endpoints.

This router exposes the standard /v1/chat/completions endpoint
and proxies requests to Meta AI Muse Spark.
"""

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from muse_meta.config import Settings
from muse_meta.models.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from muse_meta.security import enforce_rate_limit, get_settings, require_api_key
from muse_meta.services.muse_client import MuseClient, get_muse_client

router = APIRouter(
    prefix="/v1",
    tags=["chat"],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


def _get_client(dep_settings: Settings = Depends(get_settings)) -> MuseClient:
    """Return a configured MuseClient for dependency injection.

    Args:
        dep_settings: Injected application settings.

    Returns:
        A MuseClient ready to communicate with the upstream API.
    """
    return get_muse_client(dep_settings)


@router.get(
    "/models",
    status_code=status.HTTP_200_OK,
    summary="List available models",
)
async def list_models() -> dict[str, object]:
    """Return the OpenAI-compatible model list."""
    return {
        "object": "list",
        "data": [
            {
                "id": "muse-spark",
                "object": "model",
                "created": 0,
                "owned_by": "meta",
            },
        ],
    }


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
) -> AsyncGenerator[str, None]:
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
