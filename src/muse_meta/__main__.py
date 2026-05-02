"""Entry point for running the application via `python -m muse_meta`."""

import uvicorn

from muse_meta.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "muse_meta.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
