"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment-based configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    secret_key: str = Field(..., min_length=32)
    debug: bool = False

    # API
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Database
    database_url: PostgresDsn

    # GitHub
    github_webhook_secret: str
    github_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key_path: str | None = None
    github_api_base_url: str = "https://api.github.com"

    # Backend base URL (used to build callback URLs for external services)
    base_url: str = "https://ba36-85-114-192-246.ngrok-free.app"

    # AI Provider
    ai_provider: str = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4"
    openai_base_url: str = "https://api.openai.com/v1"
    ai_max_tokens: int = 4000
    ai_temperature: float = 0.7

    # Application base URL (used in emails and links sent to users)
    app_base_url: str = "http://localhost:8000"

    # Resend (https://resend.com) — preferred transactional email provider
    resend_api_key: str | None = None
    resend_from_address: str = "noreply@example.com"

    # Brevo (https://brevo.com) — alternative transactional email provider
    brevo_api_key: str | None = None
    brevo_from_address: str = "noreply@example.com"

    # SMTP (all optional — if smtp_host is unset, email channel is disabled)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "noreply@example.com"
    smtp_use_tls: bool = True

    # Scheduler
    scheduler_enabled: bool = True

    # Outbox
    outbox_batch_size: int = 1
    outbox_poll_interval: int = 10
    outbox_max_retries: int = 5
    outbox_retry_backoff_seconds: int = 60

    # Repository workspace
    workspace_dir: str = Field(
        default="/tmp/repos",
        description="Directory where cloned repositories are stored"
    )

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        """Check if running in test mode."""
        return self.environment == "test"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
