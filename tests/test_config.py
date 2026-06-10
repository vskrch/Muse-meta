"""Configuration validation tests."""

import pytest
from pydantic import ValidationError

from muse_meta.config import Settings


def test_production_requires_inbound_api_key() -> None:
    with pytest.raises(ValidationError, match="API_KEY or API_KEYS"):
        Settings(
            _env_file=None,
            environment="production",
            allowed_hosts="api.example.com",
        )


def test_production_rejects_wildcard_hosts() -> None:
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        Settings(
            _env_file=None,
            environment="production",
            api_key="token",
            allowed_hosts="*",
        )


def test_credentialed_wildcard_cors_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Wildcard CORS"):
        Settings(
            _env_file=None,
            cors_allow_origins="*",
            cors_allow_credentials=True,
        )
