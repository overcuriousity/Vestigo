"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """TraceSignal settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TS_",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me-in-production"
    allow_online: bool = False

    # Metadata store
    postgres_url: str = "postgresql+asyncpg://tracesignal:tracesignal@localhost:5432/tracesignal"

    # Event store
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "tracesignal"
    clickhouse_username: str = "default"
    clickhouse_password: str = ""

    # Vector store
    qdrant_url: str | None = Field(default="http://localhost:6333")
    qdrant_path: str | None = Field(default=None)
    qdrant_api_key: str | None = Field(default=None)
    qdrant_collection_prefix: str = "tracesignal"

    # Embeddings
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 64
    # When set, embeddings are computed via this OpenAI-compatible endpoint
    # (POST {embedding_api_base_url}/embeddings) instead of a local model.
    # embedding_model is used as the request's "model" field in that case.
    embedding_api_base_url: str | None = None
    embedding_api_key: str | None = None

    # Statistical anomaly detection
    # Maximum occurrence count below which a value is flagged as rare (value_novelty).
    stat_rarity_floor: int = 3
    # Z-score threshold for flagging a frequency window as anomalous.
    stat_z_threshold: float = 2.5
    # Number of time buckets for frequency analysis (same math as histogram).
    stat_frequency_buckets: int = 60
    # Default per-field limit when scanning for rare values.
    stat_per_field_limit: int = 25

    # Source file retention
    source_retention_path: str = "data/sources"

    # Maximum accepted source-upload size in bytes; 0 disables the limit.
    # Default 10 GiB — generous for single timeline exports while still
    # bounding how much disk one request can consume (uploads are copied to a
    # temp file plus a retained content-addressed copy).
    max_upload_bytes: int = Field(default=10 * 1024**3, ge=0)

    # Authentication: local admin bootstrap
    # Seeds the first administrator on startup if no users exist yet. The
    # seeded password is one-time: the admin is forced to rotate it on first
    # login (User.must_change_password), which invalidates this env value.
    admin_username: str = "admin"
    admin_password: str | None = None

    # Authentication: sessions
    session_ttl_hours: int = 168
    auth_cookie_name: str = "tv_session"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"

    # Authentication: audit log
    audit_enabled: bool = True

    # Authentication: optional OIDC (e.g. Authentik, Nextcloud). Independent
    # of `allow_online` — this talks to an operator-configured IdP the analyst
    # chose to trust, not an unconditional external call.
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = "openid email profile"
    oidc_redirect_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
