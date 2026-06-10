"""Application configuration using Pydantic Settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    """Split comma-separated environment values into normalized strings."""
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Muse Meta"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    docs_enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    api_key: str = ""
    api_keys: str = ""
    require_api_key: bool = True

    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    cors_allow_origins: str = ""
    cors_allow_credentials: bool = False

    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    rate_limit_requests: int = Field(default=60, ge=0)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    meta_username: str = ""
    meta_password: str = ""
    meta_ai_datr: str = ""
    meta_ai_ecto_1_sess: str = ""
    meta_ai_abra_sess: str = ""
    meta_ai_access_token: str = ""
    meta_ai_chat_doc_id: str = ""
    meta_ai_chat_doc_id_alt: str = ""
    request_timeout: float = Field(default=60.0, gt=0.0)

    state_dir: Path = Path(".state")
    cookie_file: Path | None = None

    @property
    def is_production(self) -> bool:
        """Return whether production-only safeguards should apply."""
        return self.environment == "production"

    @property
    def resolved_api_keys(self) -> list[str]:
        """Return configured inbound API keys without empty values."""
        keys = []
        if self.api_key:
            keys.append(self.api_key)
        keys.extend(_split_csv(self.api_keys))
        return list(dict.fromkeys(keys))

    @property
    def resolved_allowed_hosts(self) -> list[str]:
        """Return the TrustedHostMiddleware allowlist."""
        return _split_csv(self.allowed_hosts)

    @property
    def resolved_cors_origins(self) -> list[str]:
        """Return the CORS origin allowlist."""
        return _split_csv(self.cors_allow_origins)

    @property
    def cookie_file_path(self) -> Path:
        """Return the persisted upstream-cookie state file."""
        return self.cookie_file or self.state_dir / "meta_ai_cookies.json"

    @property
    def has_upstream_auth(self) -> bool:
        """Return whether upstream Meta AI credentials are configured."""
        return any(
            (
                self.meta_ai_access_token,
                self.meta_ai_datr,
                self.meta_ai_ecto_1_sess,
                self.meta_ai_abra_sess,
            )
        )

    @model_validator(mode="after")
    def validate_security_posture(self) -> "Settings":
        """Reject production configurations that are unsafe by construction."""
        cors_origins = self.resolved_cors_origins
        allowed_hosts = self.resolved_allowed_hosts

        if "*" in cors_origins and self.cors_allow_credentials:
            msg = "Wildcard CORS cannot be combined with credentials."
            raise ValueError(msg)

        if not self.is_production:
            return self

        if self.debug:
            msg = "DEBUG must be false in production."
            raise ValueError(msg)
        if not self.require_api_key:
            msg = "REQUIRE_API_KEY must be true in production."
            raise ValueError(msg)
        if not self.resolved_api_keys:
            msg = "API_KEY or API_KEYS must be set in production."
            raise ValueError(msg)
        if not allowed_hosts or "*" in allowed_hosts:
            msg = "ALLOWED_HOSTS must be explicit in production."
            raise ValueError(msg)
        return self


# Global settings instance for dependency injection
settings = Settings()
