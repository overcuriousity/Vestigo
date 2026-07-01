"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """TraceVector settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TV_",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me-in-production"
    allow_online: bool = False

    # Metadata store
    postgres_url: str = "postgresql+asyncpg://tracevector:tracevector@localhost:5432/tracevector"

    # Event store
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "tracevector"
    clickhouse_username: str = "default"
    clickhouse_password: str = ""

    # Vector store
    qdrant_url: str | None = Field(default="http://localhost:6333")
    qdrant_path: str | None = Field(default=None)
    qdrant_collection_prefix: str = "tracevector"

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


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
