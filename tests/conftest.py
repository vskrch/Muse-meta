"""Shared pytest fixtures."""

from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

from muse_meta.config import Settings
from muse_meta.main import create_app
from muse_meta.models.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Usage,
)
from muse_meta.routers.chat import _get_client


class FakeMuseClient:
    """Small fake client that avoids upstream Meta AI calls in tests."""

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            id="chatcmpl-test",
            created=0,
            model=request.model,
            choices=[
                Choice(
                    message=ChatMessage(role="assistant", content="ok"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(),
        )

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator:
        yield self.chat_completion(request)


@pytest.fixture
def app_settings() -> Settings:
    """Return deterministic settings isolated from the developer .env file."""
    return Settings(
        _env_file=None,
        environment="test",
        api_key="test-token",
        allowed_hosts="testserver",
        max_request_bytes=512,
        rate_limit_requests=0,
    )


@pytest.fixture
def client(app_settings: Settings) -> TestClient:
    """Return a configured FastAPI test client."""
    app = create_app(app_settings)
    app.dependency_overrides[_get_client] = lambda: FakeMuseClient()
    return TestClient(app)
