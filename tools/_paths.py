"""Shared paths for local Meta AI extraction tools."""

import os
from pathlib import Path


def state_dir() -> Path:
    """Return the local state directory for generated sensitive artifacts."""
    path = Path(os.environ.get("MUSE_STATE_DIR", ".state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_file() -> Path:
    """Return where extracted cookie state should be written."""
    return state_dir() / "meta_ai_cookies.json"


def profile_dir() -> Path:
    """Return where Playwright browser profile data should be stored."""
    configured = os.environ.get("MUSE_PLAYWRIGHT_PROFILE_DIR")
    if configured:
        path = Path(configured)
    else:
        path = state_dir() / "playwright_profile"
    path.mkdir(parents=True, exist_ok=True)
    return path.absolute()
