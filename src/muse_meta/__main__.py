"""Entry point for running the application via `python -m muse_meta`."""

import uvicorn

from muse_meta.config import settings


def main() -> None:
    """Run the ASGI server using the current environment settings."""
    uvicorn.run(
        "muse_meta.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug and not settings.is_production,
    )


if __name__ == "__main__":
    main()
