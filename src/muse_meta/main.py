"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from muse_meta.config import Settings, settings
from muse_meta.middleware import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
from muse_meta.routers.chat import router as chat_router
from muse_meta.security import get_settings
from muse_meta.services.muse_client import get_muse_client

logger = logging.getLogger(__name__)


def _build_lifespan(
    app_settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a lifespan context bound to a specific settings object."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Manage application startup and shutdown lifecycle."""
        get_muse_client(app_settings)
        yield
        client = get_muse_client(app_settings)
        await client.close()

    return lifespan


def create_app(app_settings: Settings = settings) -> FastAPI:
    """Create a configured FastAPI application instance."""
    docs_url = "/docs" if app_settings.docs_enabled else None
    redoc_url = "/redoc" if app_settings.docs_enabled else None
    openapi_url = "/openapi.json" if app_settings.docs_enabled else None

    application = FastAPI(
        title=app_settings.app_name,
        description=(
            "A reverse proxy that exposes Meta AI Muse Spark chat completions "
            "through an OpenAI-compatible API."
        ),
        version="0.2.0",
        debug=app_settings.debug and not app_settings.is_production,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=_build_lifespan(app_settings),
    )

    application.dependency_overrides[get_settings] = lambda: app_settings
    _install_middleware(application, app_settings)
    application.include_router(chat_router)
    _install_health_routes(application, app_settings)
    _install_exception_handlers(application)
    return application


def _install_middleware(application: FastAPI, app_settings: Settings) -> None:
    """Install production baseline middleware."""
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=app_settings.max_request_bytes,
    )

    allowed_hosts = app_settings.resolved_allowed_hosts
    if allowed_hosts:
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_hosts,
        )

    cors_origins = app_settings.resolved_cors_origins
    if cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=app_settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )


def _install_health_routes(application: FastAPI, app_settings: Settings) -> None:
    """Install unauthenticated liveness and readiness endpoints."""

    @application.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Return service liveness status."""
        return {"status": "ok"}

    @application.get("/ready", tags=["health"])
    async def readiness_check() -> dict[str, object]:
        """Return readiness status without calling the upstream service."""
        client = get_muse_client(app_settings)
        if not client.is_auth_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Meta AI upstream authentication is not configured.",
            )

        return {
            "status": "ok",
            "upstream": client.health_snapshot(),
        }


def _install_exception_handlers(application: FastAPI) -> None:
    """Install generic exception handling for production responses."""

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Hide internal tracebacks from clients while logging details."""
        logger.exception(
            "Unhandled request failure: %s %s",
            request.method,
            request.url,
        )
        return JSONResponse(
            {"detail": "Internal server error."},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


app = create_app()
