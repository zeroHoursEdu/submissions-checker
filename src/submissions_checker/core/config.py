"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, PostgresDsn, field_validator, model_validator
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

    # Credential-endpoint throttling (per process, per client IP + username).
    login_max_attempts: int = 10
    login_window_seconds: int = 900

    # API
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Database
    database_url: PostgresDsn
    # Connection pool per application process. Sized so that
    # replicas * (pool + overflow) stays under the server's max_connections —
    # the production stack runs two replicas against max_connections=30.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # AI Provider — exactly one is active at a time, selected by `ai_provider`.
    ai_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    ai_max_tokens: int = 4000

    # Application base URL (used in emails and links sent to users)
    app_base_url: str = "http://localhost:8000"

    # Resend (https://resend.com) — preferred transactional email provider
    resend_api_key: str | None = None
    resend_from_address: str = "noreply@example.com"

    # Brevo (https://brevo.com) — alternative transactional email provider
    brevo_api_key: str | None = None
    brevo_from_address: str = "noreply@example.com"

    # Air-raid pause (https://devs.alerts.in.ua). Without a token the pause button reports
    # that the check is unavailable and no attempt is ever paused — an unverified claim must
    # not stop a graded clock. The upstream rate limit is roughly a dozen requests per
    # minute per IP, so responses are cached; at 30s and two app replicas that is 4/min.
    alerts_in_ua_token: str | None = None
    alerts_in_ua_base_url: str = "https://api.alerts.in.ua"
    air_raid_pause_enabled: bool = True
    air_raid_cache_seconds: int = 30

    # SMTP (all optional — if smtp_host is unset, email channel is disabled)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "noreply@example.com"
    smtp_use_tls: bool = True

    # Scheduler
    scheduler_enabled: bool = True

    # Teacher digest notifications — coalesce review-queue emails per teacher
    teacher_digest_enabled: bool = True
    teacher_digest_window_seconds: int = 120  # max wait since oldest pending entry before flushing
    teacher_digest_max_batch: int = 25  # eager flush threshold (whole-group burst)
    teacher_digest_flush_interval: int = 30  # scheduler poll interval for the flush job

    # Outbox
    # Messages per poll. The loop is sequential either way, so a bigger batch only
    # removes the poll interval between messages; at 1, a handful of failing email
    # retries starved RUN_CHECKS for minutes in the e2e stack.
    outbox_batch_size: int = 20
    outbox_poll_interval: int = 10
    outbox_max_retries: int = 5
    outbox_retry_backoff_seconds: int = 60

    # Metrics: how often the DB-derived gauges (students, backlogs, outbox age) are recomputed.
    metrics_refresh_interval: int = 60

    # Subject gradebook stats: how often the cached stat-card numbers (average
    # mark, pass %, pending review, cheating %) are recomputed per subject.
    subject_stats_refresh_interval: int = 300

    # Deadline reminders: students with no submission get one email when an
    # assignment's deadline is within this many days; the job runs on this interval.
    # Off by default: the first run on an existing deployment would email every
    # student with an unsubmitted assignment due this week, so an operator turns it
    # on deliberately.
    deadline_reminders_enabled: bool = False
    deadline_reminder_days_before: int = 2
    deadline_reminder_interval: int = 3600

    # Subject plugins directory (gitignored; each subdirectory is a plugin with config.yml)
    plugins_dir: str = "plugins"

    # Host-absolute path to the plugins directory, used only for Docker-in-Docker sandbox bind
    # mounts (the host daemon can't resolve plugins_dir's container-relative path). Falls back
    # to plugins_dir when unset.
    host_plugins_dir: str | None = None

    # Upper bound on the sandbox resources a subject's config.yml may request. A subject
    # declaring more than the host can give is clamped to these values rather than being
    # honoured, so one subject cannot exhaust the host for every other subject.
    # Docker-style size string ("256m", "1g") and a CPU share.
    sandbox_max_memory: str = "512m"
    sandbox_max_cpus: float = 1.0

    # S3-compatible object storage (images and assignment content files)
    s3_bucket_name: str = "submissions-checker"
    s3_endpoint_url: str | None = None  # set to http://localstack:4566 in dev
    s3_public_base_url: str | None = None  # base URL for constructing public file URLs
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"

    # Proctoring recording-consent notice (configurable for jurisdiction). None falls
    # back to the localized default in the active i18n vocabulary (consent.notice_text).
    recording_consent_notice: str | None = None

    @field_validator("secret_key")
    @classmethod
    def _reject_placeholder_secret_key(cls, value: str) -> str:
        """Reject known placeholder/weak secret keys in any environment."""
        denylist = {
            "your-secret-key-here-change-in-production",
            "changeme",
            "secret",
        }
        if value.lower() in denylist or "your-secret-key" in value.lower():
            raise ValueError(
                "SECRET_KEY is set to a known placeholder/weak value. "
                "Set a strong random key, e.g. `openssl rand -hex 32`."
            )
        return value

    @model_validator(mode="after")
    def _refuse_debug_in_production(self) -> Self:
        """FastAPI's debug mode returns tracebacks to the client. Never in production."""
        if self.environment == "production" and self.debug:
            raise ValueError("DEBUG must be false when ENVIRONMENT=production")
        return self

    @property
    def cookie_secure(self) -> bool:
        """Whether auth cookies should set the Secure flag (production only)."""
        return self.is_production

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
    # Every field is populated from the environment or its default; the generated
    # __init__ signature does not model that, hence the ignore.
    return Settings()  # type: ignore[call-arg]
