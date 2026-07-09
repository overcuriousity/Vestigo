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
    allow_online: bool = False

    # Login backoff: after `threshold` consecutive failures per
    # (username, client IP), attempts are rejected with 429 for
    # base * 2**(n - threshold) seconds, capped at max.
    login_backoff_threshold: int = 5
    login_backoff_base_seconds: float = 2.0
    login_backoff_max_seconds: float = 300.0

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
    # Charset detector's own rarity floor: a character appearing in this many or
    # fewer *distinct values* is treated as rare (self-baseline mode). Distinct
    # from stat_rarity_floor (which counts value *occurrences*, not chars) so the
    # two detectors can be tuned independently; defaults to the same value.
    stat_charset_rarity_floor: int = 3
    # Z-score threshold for flagging a frequency window as anomalous.
    stat_z_threshold: float = 2.5
    # Number of time buckets for frequency analysis (same math as histogram).
    stat_frequency_buckets: int = 60
    # Default per-field limit when scanning for rare values.
    stat_per_field_limit: int = 25
    # Minimum backwards jump (seconds) before the timestamp-order detector
    # flags a record — suppresses sub-second logger jitter. 0 = AMiner-strict.
    stat_order_min_skew: float = 1.0
    # Proportion-shift detector (G-test): Benjamini-Hochberg false-discovery-rate
    # ceiling — "of everything flagged, at most this fraction expected false".
    stat_shift_fdr_q: float = 0.05
    # Proportion-shift effect-size floor: a value's suspect-window share must
    # differ from its baseline share by at least this factor (either direction)
    # to be reported even when statistically significant — on large timelines
    # significance without magnitude is noise.
    stat_shift_min_ratio: float = 2.0
    # Per-field cap on candidate values the proportion-shift scan fetches from
    # ClickHouse (highest total volume first). Hitting the cap understates the
    # BH test count for that field; the run carries a warning when it happens.
    stat_shift_max_candidates_per_field: int = 2000
    # Guardrails for whole-corpus detector/inventory scans (the shared SETTINGS
    # clause every heavy GROUP BY carries). Defaults sized for the session-27
    # 300M-row incident; tune per ClickHouse host RAM/cores. See db/_scan.py.
    stat_scan_max_threads: int = 8
    stat_scan_external_group_by_bytes: int = 4_000_000_000
    stat_scan_max_memory_bytes: int = 12_000_000_000

    # Ingestion
    # Events per ClickHouse insert during ingestion. Each batch is one HTTP
    # round-trip, so larger batches amortize LAN latency and ClickHouse's
    # per-insert part-creation cost (official guidance: 10k-100k rows per
    # insert). Memory trade-off: a batch is held as parsed Event objects plus
    # a column-oriented copy at insert time — at ~2-4 KB per event, 20k rows
    # peak around 50-150 MB transiently. Raise for fast networks and wide
    # memory headroom, lower for constrained hosts.
    ingest_batch_size: int = Field(default=20_000, ge=1)

    # Source file retention
    source_retention_path: str = "data/sources"

    # Maximum accepted source-upload size in bytes; 0 disables the limit.
    # Default 10 GiB — generous for single timeline exports while still
    # bounding how much disk one request can consume (uploads are copied to a
    # temp file plus a retained content-addressed copy).
    max_upload_bytes: int = Field(default=10 * 1024**3, ge=0)

    # Enrichers: where admin-uploaded enricher assets (e.g. the MaxMind
    # GeoLite2 database) are stored.
    enricher_data_path: str = "data/enrichers"
    # Events read per ClickHouse round-trip while an enrichment job scans a
    # source. Enrichment is I/O/round-trip bound, not model-bound like
    # embedding — every event must be scanned and matched regardless of how
    # many carry an enrichable value — so this pages far larger than the
    # embedding batch (default matches ingest_batch_size). On a 180M-event
    # timeline the difference is ~9k vs ~180k HTTP round-trips. Kept separate
    # from embedding_batch_size (memory-bound by the model) on purpose.
    enrichment_batch_size: int = Field(default=20_000, ge=1)

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
