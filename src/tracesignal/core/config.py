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
    # Interval-periodicity detector (cadence): BH false-discovery-rate ceiling,
    # shared across both directions (missed cadence + new regularity).
    stat_interval_fdr_q: float = 0.05
    # Cadence-break effect floor: the suspect window's arrival rate must differ
    # from the baseline rate by at least this factor (either direction) to be
    # reported even when statistically significant.
    stat_interval_min_rate_ratio: float = 2.0
    # A value needs at least this many baseline inter-arrival intervals before
    # its cadence is considered learned (direction a) — fewer than this also
    # marks the baseline "sparse" for the beaconing gate (direction b).
    stat_interval_min_baseline_intervals: int = 5
    # Baseline delta-CV at or below which a value counts as "regular" and is
    # eligible for cadence-break testing. 1.0 is a Poisson process; 0.5 demands
    # visibly periodic behavior. The gap up to stat_interval_cv_irregular_min
    # is a deliberate dead band — neither direction tests those values.
    stat_interval_cv_regular_max: float = 0.5
    # Baseline delta-CV at or above which a value counts as "irregular/bursty"
    # and is eligible for beaconing (new-regularity) testing.
    stat_interval_cv_irregular_min: float = 0.8
    # Minimum suspect-window intervals before the Greenwood spacing statistic's
    # normal approximation is trusted for a beaconing test.
    stat_interval_beacon_min_intervals: int = 10
    # Beaconing effect floor: the suspect window's delta-CV must be at or below
    # this ("period ± small jitter") to be reported.
    stat_interval_beacon_cv_max: float = 0.3
    # Beaconing span floor: the value's active span (first..last arrival) must
    # cover at least this fraction of the suspect window — a short dense burst
    # of evenly spaced events must not read as beaconing.
    stat_interval_beacon_min_span: float = 0.5
    # Per-field cap on candidate values the interval scan fetches (highest
    # total volume first); same warning semantics as the proportion-shift cap.
    stat_interval_max_candidates_per_field: int = 2000
    # Value-distribution-drift detector (D9): BH false-discovery-rate ceiling
    # shared by both test branches (KS numeric / k-category G-test).
    stat_drift_fdr_q: float = 0.05
    # KS effect floor — minimum D statistic (max CDF gap) for a finding.
    stat_drift_min_ks_d: float = 0.1
    # Categorical effect floor — minimum total-variation distance.
    stat_drift_min_tvd: float = 0.05
    # Minimum field-bearing events on each side of a test; smaller sides are
    # skipped (excluded from the FDR pool) with a warning.
    stat_drift_min_samples: int = 20
    # Sequence-novelty detector: default n-gram length (AMiner
    # EventSequenceDetector's default sequence length).
    # Constrained here so a bad TS_STAT_SEQUENCE_NGRAM fails at startup as a
    # config error instead of surfacing as a 422 that blames the client.
    stat_sequence_ngram: int = Field(default=3, ge=2, le=5)
    # Cap on novel n-grams fetched per run (lowest suspect volume first —
    # rarest sequences are the detector's point); hitting it carries a warning.
    stat_sequence_max_candidates: int = 2000
    # Guardrails for whole-corpus detector/inventory scans (the shared SETTINGS
    # clause every heavy GROUP BY carries). Defaults sized for the session-27
    # 300M-row incident; tune per ClickHouse host RAM/cores. See db/_scan.py.
    stat_scan_max_threads: int = 8
    stat_scan_external_group_by_bytes: int = 4_000_000_000
    # Spill threshold for plain ORDER BY sorts. Window-function sorts cannot
    # spill (ClickHouse limitation, docs/ANOMALY_DETECTION.md) — those scans
    # are bounded structurally instead.
    stat_scan_external_sort_bytes: int = 4_000_000_000
    # Total memory budget for heavy scans, shared across concurrent scans:
    # each query's max_memory_usage is budget / concurrency. 0 (default) =
    # auto: memory-ratio × detected RAM (cgroup limit when containerized,
    # physical RAM otherwise; see db/_scan.py). Set a nonzero value to pin
    # it — required when ClickHouse runs on a different host than the app
    # (size it to *that* host's RAM, leaving headroom for the server's own
    # caches/merges — ~70% of its RAM is a good start).
    stat_scan_max_memory_bytes: int = 0
    # Fraction of detected memory the auto budget uses.
    stat_scan_memory_ratio: float = Field(default=0.8, gt=0, le=1)
    # Max detector scans running against ClickHouse at once. Surplus scans
    # queue on a semaphore (db/_scan.py::HEAVY_SCAN_GATE). Without this, N
    # parallel detector requests each carry the full per-query cap and can
    # stack past the ClickHouse host's RAM — observed as a kernel OOM-kill
    # of clickhouse-server, not a clean query error.
    stat_scan_concurrency: int = Field(default=2, ge=1)
    # Max entries in the process-local baseline-compare layer cache
    # (db/viz_cache.py, M24c) — memoizes the unfiltered baseline layer of
    # Visualize compare renders so it isn't a full-timeline re-scan on every
    # filtered render. 0 disables caching entirely. Entries are small
    # bounded aggregates; freshness is keyed, not TTL'd.
    viz_baseline_cache_entries: int = Field(default=64, ge=0)

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
