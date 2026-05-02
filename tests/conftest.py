"""Shared pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

from muse_meta.main import app


@pytest.fixture
def client() -> TestClient:
    """Return a configured FastAPI test client."""
    return TestClient(app)
