"""Application configuration using Pydantic Settings.

Follows PEP 20: Explicit is better than implicit.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Attributes:
        app_name: The name of the application.
        debug: Enable debug mode for development.
        host: Host to bind the server to.
        port: Port to bind the server to.
        muse_base_url: Base URL for the Meta AI Muse Spark API.
        muse_api_key: API key for authenticating with Meta AI Muse Spark.
        request_timeout: Timeout in seconds for upstream requests.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Muse Meta"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    meta_username: str = ""
    meta_password: str = ""
    meta_ai_datr: str = ""
    meta_ai_ecto_1_sess: str = ""
    meta_ai_abra_sess: str = ""
    meta_ai_access_token: str = ""
    request_timeout: float = 60.0


# Global settings instance for dependency injection
settings = Settings()
