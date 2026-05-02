"""FastAPI application entry point.

Simple is better than complex (PEP 20).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from muse_meta.config import settings
from muse_meta.routers.chat import router as chat_router
from muse_meta.services.muse_client import get_muse_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle.

    Args:
        app: The FastAPI application instance.

    Yields:
        None: Control is yielded to the application during its lifetime.
    """
    # Startup: initialize shared resources
    get_muse_client(settings)
    yield
    # Shutdown: clean up resources
    client = get_muse_client(settings)
    await client.close()


app = FastAPI(
    title=settings.app_name,
    description=(
        "A reverse proxy that exposes Meta AI Muse Spark chat completions "
        "through an OpenAI-compatible API."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Allow cross-origin requests for frontend clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(chat_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Return service health status.

    Returns:
        A simple JSON object indicating the service is running.
    """
    return {"status": "ok"}
