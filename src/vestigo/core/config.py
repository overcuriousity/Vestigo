"""Application configuration: environment variables plus DB-backed overrides.

Two layers, resolved per field. The environment (``VESTIGO_*``, optionally via
``.env``) is the deploy-time layer; the ``app_settings`` table is the runtime
layer an admin edits from the web console without a restart. **Environment
always wins**: a field the operator pinned in the environment ignores any
stored override, so a locked-down deployment stays locked down.

:func:`get_settings` returns the merged view and is what the whole application
calls. The merge is cheap (a cached ``model_copy``) and synchronous, because
the DB layer is not read here — it is loaded once at startup and re-applied
whenever an admin saves (:func:`set_runtime_overrides`, driven by
``core/runtime_settings.py``). Overrides are process-local, matching the
single-process deployment model that ``core/jobs.py`` already assumes.

Which fields may be overridden, and how they are presented, is declared in
``core/settings_registry.py`` — not here.
"""

from collections.abc import Mapping
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Vestigo settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="VESTIGO_",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    allow_online: bool = False

    # Where secrets edited in the admin console may live. "db" (default):
    # admins may store passwords/API keys in the app_settings table (plaintext
    # at rest — acceptable only if Postgres itself is trusted). "env-only":
    # the settings API refuses to store any secret and the resolver ignores
    # previously stored ones, leaving the environment as the only source.
    # Deliberately env-only itself: a mode meant to constrain the console must
    # not be editable from it. Covers every secret, the LLM API key included —
    # its agent-scoped predecessor `agent_secret_mode` was retired with the
    # separate agent settings row (migration 0033).
    secrets_mode: str = Field(default="db", pattern="^(db|env-only)$")

    # Login backoff: after `threshold` consecutive failures per
    # (username, client IP), attempts are rejected with 429 for
    # base * 2**(n - threshold) seconds, capped at max.
    login_backoff_threshold: int = 5
    login_backoff_base_seconds: float = 2.0
    login_backoff_max_seconds: float = 300.0

    # Metadata store
    postgres_url: str = "postgresql+asyncpg://vestigo:vestigo@localhost:5432/vestigo"

    # Event store
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "vestigo"
    clickhouse_username: str = "default"
    clickhouse_password: str = ""

    # Vector store
    qdrant_url: str | None = Field(default="http://localhost:6333")
    qdrant_path: str | None = Field(default=None)
    qdrant_api_key: str | None = Field(default=None)
    qdrant_collection_prefix: str = "vestigo"

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
    # Constrained here so a bad VESTIGO_STAT_SEQUENCE_NGRAM fails at startup as a
    # config error instead of surfacing as a 422 that blames the client.
    stat_sequence_ngram: int = Field(default=3, ge=2, le=5)
    # Cap on novel n-grams fetched per run (lowest suspect volume first —
    # rarest sequences are the detector's point); hitting it carries a warning.
    stat_sequence_max_candidates: int = 2000
    # Sequence-motif detector: minimum occurrences before an n-gram counts as
    # a recurring motif. 2 would surface every coincidental repeat.
    stat_motif_min_support: int = Field(default=3, ge=2)
    # Cap on candidate motifs fetched per source (highest support first);
    # hitting it carries a warning.
    stat_motif_max_candidates: int = 1000
    # Only the top-K merged candidates by support get the second cadence
    # pass — bounds the PARTITION BY gram window sort (can't spill).
    stat_motif_cadence_top_k: int = 500
    # ── Analysis gate ────────────────────────────────────────────────────────
    # Structural preconditions deciding which methods the Investigate rail
    # offers up front (db/analysis_plan.py). A method is gated off only when it
    # *cannot* produce a finding on the data — never when it is merely unlikely
    # to — and a gated method is always still runnable on request. Raising any
    # of these therefore costs coverage of the "offered automatically" set, not
    # reachability.
    #
    # Share of a field's sampled values that must parse as a number before the
    # numeric-range band has anything to learn.
    analysis_gate_min_numeric_ratio: float = Field(default=0.9, gt=0, le=1)
    # A field with at most this many distinct values is enum-like: its learned
    # alphabet is the union of a handful of literals and every value is drawn
    # from it, so charset novelty cannot fire. Entropy is deliberately not
    # gated on this — its band is a comparison, and one enum literal can sit
    # far outside it.
    analysis_gate_max_enum_distinct: int = Field(default=5, ge=1)
    # Distinct series values needed before two n-grams can differ at all. One
    # value yields a single repeated n-gram; two already yield 2**n.
    analysis_gate_min_series_distinct: int = Field(default=2, ge=2)
    # Seconds of span a timeline must cover before frequency bucketing is
    # meaningful — below this the buckets it splits into (stat_frequency_buckets)
    # are narrower than a second and collapse into each other.
    analysis_gate_min_frequency_buckets: int = Field(default=12, ge=2)
    # Repeats a series value needs before an inter-arrival cadence can be fitted.
    analysis_gate_min_interval_periods: int = Field(default=3, ge=2)
    # Cached method results retained per case, least-recently-computed evicted
    # first. Every row is derived data: eviction costs a rescan and nothing else.
    analysis_cache_max_rows_per_case: int = Field(default=500, ge=1)
    # Guardrails for whole-corpus detector/inventory scans (the shared SETTINGS
    # clause every heavy GROUP BY carries). Defaults sized for the session-27
    # 300M-row incident; tune per ClickHouse host RAM/cores. See db/_scan.py.
    # 0 = auto: an even share of the cores ClickHouse reports for itself
    # (cores / concurrency, floor 2), read from the server at startup. A
    # nonzero value pins it, exactly as stat_scan_max_memory_bytes pins the
    # budget. It was the constant 8, which is 40% of a 20-core host and 4x
    # oversubscription of a 4-core one.
    stat_scan_max_threads: int = 0
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
    # Fraction of the ClickHouse ceiling *minus its own caches* that the auto
    # budget uses; the remainder is merge and allocator-slack headroom.
    stat_scan_memory_ratio: float = Field(default=0.8, gt=0, le=1)
    # Max detector scans running against ClickHouse at once. Surplus scans
    # queue on a semaphore (db/_scan.py::HEAVY_SCAN_GATE). Without this, N
    # parallel detector requests each carry the full per-query cap and can
    # stack past the ClickHouse host's RAM — observed as a kernel OOM-kill
    # of clickhouse-server, not a clean query error.
    stat_scan_concurrency: int = Field(default=2, ge=1)
    # Write-side guardrail for the enrichment partition rewrite
    # (db/clickhouse.py::finalize_enrichment_apply). The stat_scan_* settings
    # above bound a *scan*; the rewrite also INSERTs a full copy of the
    # source's partition, which is the query shape that OOM-killed a 32 GiB
    # full-docker host (session-56 incident). ClickHouse's own
    # max_insert_threads is deliberately left alone: its default (0) means a
    # single-threaded INSERT SELECT, and raising it would give every thread
    # its own squashing buffer — more write-side memory on exactly the query
    # we are trying to bound, to speed up a path that runs once per source at
    # job end and is never latency-critical.
    #
    # min_insert_block_size_bytes is a squash *floor*, not a cap on in-flight
    # block size: rows are accumulated until a block reaches at least this many
    # bytes before a part is written. Lowering it trades more (smaller) parts
    # and more background merge work for less insert-time memory. 64 MiB sits
    # deliberately under ClickHouse's own 256 MiB default — this path buys
    # headroom with throughput.
    enrichment_apply_insert_block_bytes: int = Field(default=67_108_864, ge=1_048_576)
    # Seconds the enrichment apply keeps its scan-gate slot after the partition
    # swap, waiting for the merges the swap queued to finish. Merge memory is
    # the one consumer max_memory_usage cannot bound, so releasing the slot at
    # the ALTER admits the next detector sweep straight into the merge burst.
    # Bounded and non-fatal: the apply is already durably swapped in, so a slow
    # merge must never fail it. 0 skips the wait — correct when ClickHouse has
    # a max_server_memory_usage of its own, which bounds merges at the layer
    # that can actually see them (docs/DEPLOYMENT.md "Resource sizing").
    enrichment_apply_merge_wait_seconds: int = Field(default=300, ge=0)
    # Seconds a value-inventory export waits for the single streamed-scan slot
    # (db/_scan.py::EXPORT_SCAN_GATE) before the request is refused with 503.
    # The slot is held for the whole client-paced drain, so an analyst who
    # backgrounds or throttles a large download holds it for as long as they
    # like; waiting on it without a bound made every other export in the
    # process — every case, every user — block indefinitely, each one parked on
    # an anyio worker thread that the rest of the app also needs. Bounded, the
    # worst case is a queued export occupying a thread for this long and then
    # telling the analyst to retry. 0 refuses immediately when the slot is
    # taken.
    export_scan_queue_wait_seconds: float = Field(default=30.0, ge=0)
    # Max entries in the process-local baseline-compare layer cache
    # (db/viz_cache.py, M24c) — memoizes the unfiltered baseline layer of
    # Visualize compare renders so it isn't a full-timeline re-scan on every
    # filtered render. 0 disables caching entirely. Entries are small
    # bounded aggregates; freshness is keyed, not TTL'd.
    viz_baseline_cache_entries: int = Field(default=64, ge=0)

    # Marks (Visualize): instants a single mark source may draw. A source is
    # one filter, saved view or baseline definition; past the cap the figure
    # draws the first N by time and the caption says how many it did not.
    viz_marks_max: int = Field(default=50, ge=1, le=500)

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

    # Case export/import (X1). In-flight export archives are written here
    # before download; they contain the full case, so this directory is as
    # sensitive as source_retention_path. It is created 0700 and that mode is
    # forced on an existing directory; startup fails only if the path is owned
    # by another user or is not a real directory. Size it for the largest case
    # exported, not for the average one.
    # Master switch for case export/import. Off hides the feature entirely
    # (no buttons in the UI, 503 from the router) — for deployments where a
    # whole-case archive leaving the box is a policy problem, not a feature.
    transfer_enabled: bool = True
    transfer_temp_path: str = "data/transfer"
    # Ceiling on an imported archive's total *uncompressed* size; 0 disables.
    # Events and blobs travel ZIP_STORED, so a legitimate archive expands by
    # roughly 1x and only its NDJSON members compress meaningfully — a large
    # ratio means a decompression bomb, not a big case. Checked against the
    # manifest before any member is read.
    transfer_max_expanded_bytes: int = Field(default=200 * 1024**3, ge=0)
    # Ceiling on any *single* postgres/* member of an imported archive; 0
    # disables. Separate from the total because the total says nothing about
    # one member: a lone 100 GiB NDJSON sits far under a 200 GiB total and
    # would still exhaust memory. These members hold the case's metadata (rows,
    # not events), so even a huge case stays orders of magnitude below this.
    transfer_max_metadata_bytes: int = Field(default=2 * 1024**3, ge=0)
    # Concurrent case export/import jobs across the instance; 0 disables the
    # cap. Each one can hold a multi-GiB upload plus its expansion on disk and
    # any authenticated user may start one, so the default is deliberately
    # small — this is admission control, not a throughput knob.
    transfer_max_concurrent: int = Field(default=2, ge=0)

    # Seeds a fabricated demo case into each user's case list the first time
    # they log in (once per user, ever — deleting it is final unless they
    # restore it explicitly). Off for deployments where fabricated data in a
    # case list is a policy problem.
    demo_case_enabled: bool = True
    # Concurrent demo-case builds across the instance; 0 disables the cap.
    # Generating and ingesting a quarter of a million events is CPU-bound
    # Python, so it holds the GIL and every concurrent build contends with the
    # API's own event loop. One at a time keeps a post-upgrade burst of first
    # logins from making the whole instance feel slow; raise it on a box with
    # cores to spare.
    demo_max_concurrent: int = Field(default=1, ge=0)

    # Sigma rule runner (docs/ANOMALY_DETECTION.md §13). Global ruleset
    # directory scanned for *.yml/*.yaml at run time — an offline file drop
    # (e.g. a vendored SigmaHQ clone); empty string disables the global set.
    # Rules uploaded per case live in Postgres instead.
    sigma_rules_path: str = ""
    # Postgres rows per bulk_create_annotations chunk while a Sigma run
    # persists hits. Hits stream from ClickHouse in blocks, so this bounds
    # both write-transaction size and peak memory for match-everything rules.
    sigma_annotation_batch_size: int = Field(default=5_000, ge=100, le=50_000)

    # Stories (docs/STORIES.md). An export freezes every block server-side and
    # then stores the client-rendered standalone HTML artifact, so both the
    # work and the stored bytes need a ceiling. The block cap bounds how much
    # querying one export request can trigger (resolution is synchronous); the
    # snapshot cap bounds the JSON column; the artifact cap bounds the uploaded
    # HTML, which inlines the stylesheet and every frozen row.
    story_export_max_blocks: int = Field(default=500, ge=1)
    story_export_max_snapshot_bytes: int = Field(default=64 * 1024**2, ge=0)
    story_max_artifact_bytes: int = Field(default=20 * 1024**2, ge=0)
    # Ceiling on one markdown block's text. Generous for report prose; the
    # point is that a block is embedded verbatim into every later snapshot,
    # so an unbounded one multiplies across exports.
    story_max_markdown_bytes: int = Field(default=256 * 1024, ge=1024)

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

    # AI investigation agent (optional; see docs/AGENT.md). The feature is off
    # — and invisible in the UI — unless both agent_model and an endpoint are
    # configured AND the endpoint answers a probe. Independent of
    # `allow_online` for the same reason as OIDC and the embeddings endpoint:
    # the operator explicitly points Vestigo at an endpoint they trust.
    agent_model: str | None = None
    # Wire protocol of the endpoint: "openai" = OpenAI chat-completions
    # (ollama, vllm, LocalAI, OpenRouter, api.moonshot.ai/v1), "anthropic" =
    # Anthropic Messages (Anthropic itself, Kimi coding plan
    # https://api.kimi.com/coding).
    agent_provider: str = Field(default="openai", pattern="^(openai|anthropic)$")
    agent_api_base_url: str | None = None
    agent_api_key: str | None = None
    # Some subscription endpoints gate on the client's User-Agent (e.g. Kimi's
    # /coding endpoint 403s unless the UA identifies a coding agent). Set the
    # value the endpoint expects; unset sends the SDK default.
    agent_user_agent: str | None = None
    # Extra HTTP headers as a JSON object, e.g. '{"X-Custom": "1"}'.
    agent_extra_headers: dict[str, str] | None = None
    # Hard cap on model round-trips per user message (tool-call loop bound).
    agent_max_turns: int = Field(default=15, ge=1, le=100)
    # Reasoning effort passed to the model, when the provider supports it.
    # Keep in sync with agent/config.py::EFFORT_VALUES.
    agent_reasoning_effort: str | None = Field(default=None, pattern="^(off|low|medium|high|max)$")
    # Seconds an availability probe result is cached before re-probing.
    agent_probe_ttl_seconds: float = Field(default=60.0, gt=0)
    # Wall clock for one model request inside an agent turn. A local model on
    # modest hardware is the case that needs this raised; the stranded-turn
    # sweep derives its own bound from this value times agent_max_turns.
    agent_request_timeout_seconds: float = Field(default=300.0, ge=10.0, le=3600.0)
    # Wall clock for the one-shot column suggestion (probe + resolve + request
    # together). Deliberately short: it is an advisory call on an ingest job's
    # critical path and degrades to the local scorer when it expires.
    column_advisor_timeout_seconds: float = Field(default=45.0, ge=5.0, le=600.0)
    # Model context window in tokens. Unset = no proactive sliding window
    # (the right number is model-specific, so it's an explicit opt-in; an
    # overflow still enables the window reactively for one retry).
    agent_context_window: int | None = Field(default=None, ge=1024, le=10_000_000)
    # How much of an example record tool results carry: full | message |
    # minimal | auto (derive from agent_context_window). Unset = full, i.e. a
    # deployment that has declared no constraint is assumed to have room.
    # Keep in sync with agent/fidelity.py::FIDELITY_VALUES.
    agent_tool_fidelity: str | None = Field(default=None, pattern="^(full|message|minimal|auto)$")
    # Admin hard-deny tool list as a JSON array, e.g. '["semantic_search"]'.
    # Removed from the tool server for the in-app agent AND the external
    # /mcp transport; per-user/per-chat toggles can only restrict further.
    agent_disabled_tools: list[str] | None = None

    # External MCP endpoint (/mcp): serves the same scoped tool server the
    # built-in agent uses over streamable HTTP, authenticated by scoped
    # per-timeline tokens (agent_tokens table). Off by default — invisible
    # unless the operator enables it. Independent of VESTIGO_AGENT_* (serving
    # MCP needs no LLM endpoint).
    mcp_enabled: bool = False

    # Outside-facing base URL of this deployment, e.g.
    # "https://vestigo.example.org" — scheme included, which the validator
    # below enforces because a scheme-less host is itself a relative URL. Links Vestigo hands to a consumer that is
    # not the browser are relative paths, which an external /mcp client has no
    # origin to resolve — `propose_chart`'s Visualize deep link is the one that
    # matters today, and over MCP it *is* the figure. Set this and such links
    # become absolute; unset (the default) keeps the relative form, so nothing
    # changes for a deployment that only ever serves its own SPA.
    #
    # Deliberately not derived from the request's Host header: behind a reverse
    # proxy that is whatever the proxy forwarded, and a confidently wrong link
    # is worse than a relative one the reader knows to complete.
    public_base_url: str | None = None

    # ── Generated converters (docs/INPUT_FORMATS.md §"Generated converters") ──
    # Off by default: enabling it lets LLM-authored Python run in a guarded
    # subprocess on this host. Needs a configured, reachable agent endpoint too.
    converter_generation_enabled: bool = False
    # Generation + repair rounds on the sample before giving up.
    converter_max_attempts: int = Field(default=4, ge=1, le=10)
    # Bytes of the excerpt shown to the model (head/middle/tail, whole records).
    # Small on purpose — docs/INPUT_FORMATS.md §"The loop" step 1 says why.
    converter_sample_bytes: int = Field(default=4096, ge=4096, le=1048576)
    # Wall clock for one generation or repair round — availability probe,
    # config resolution and the model request together, not just the request.
    # A local model writing a whole converter script is the slow case: when
    # every attempt dies here the job reports "no working converter" with no
    # draft to show, so this is the knob that has to be reachable.
    converter_generation_timeout_seconds: int = Field(default=180, ge=30, le=3600)
    # Wall clock for the full-file conversion run; the sample run gets min(60, this).
    converter_run_timeout_seconds: int = Field(default=600, ge=30, le=7200)
    # RLIMIT_AS for the converter subprocess. Floor measured 2026-08-17: pyarrow
    # imports at 2048 MB and fails at 1024 (OpenBLAS refuses to allocate).
    converter_run_memory_mb: int = Field(default=2048, ge=2048, le=65536)
    # RLIMIT_FSIZE for the subprocess: the produced Parquet cannot grow past this.
    converter_run_output_mb: int = Field(default=4096, ge=64, le=1048576)

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url_is_absolute(cls, value: str | None) -> str | None:
        """Refuse a base URL that an MCP client would resolve as a relative path.

        The setting exists because a consumer that is not the browser has no
        origin to complete a path against, so "vestigo.example.org" typed
        without a scheme produces exactly the confidently wrong link the field
        was added to avoid — and does it silently, since the result still looks
        like a URL. Fail at set-time instead: the settings API hands the
        message back to the admin console, and a mistyped
        ``VESTIGO_PUBLIC_BASE_URL`` stops the process rather than serving
        broken links for the life of the deployment.

        An empty string is the console's "cleared", not a bad value; it means
        the same as unset.
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "must be an absolute URL including the scheme, e.g. "
                f"'https://vestigo.example.org' (got {value!r})"
            )
        return text


@lru_cache
def get_base_settings() -> Settings:
    """Return the environment/default layer alone, with no DB overrides applied.

    This is what tells "the operator set VESTIGO_X" apart from "the field
    carries its own default": pydantic-settings only records a field in
    ``model_fields_set`` when something actually supplied it. Applying
    overrides on top would pollute that set, so env-pin checks must always ask
    this object, never the merged one.
    """
    return Settings()


#: DB-backed overrides for fields the environment did not pin. Process-local
#: and replaced wholesale by :func:`set_runtime_overrides`.
_overrides: dict[str, Any] = {}
_effective: Settings | None = None


def env_pinned(field: str) -> bool:
    """Whether the environment explicitly supplied this field."""
    return field in get_base_settings().model_fields_set


def get_settings() -> Settings:
    """Return the effective settings: environment first, then DB overrides."""
    global _effective
    if _effective is None:
        base = get_base_settings()
        applicable = {k: v for k, v in _overrides.items() if not env_pinned(k)}
        _effective = base.model_copy(update=applicable) if applicable else base
    return _effective


def set_runtime_overrides(values: Mapping[str, Any]) -> None:
    """Replace the DB-backed override layer and invalidate the merged view.

    Values are expected to have been validated already (the settings API
    validates a full candidate ``Settings`` before persisting) — ``model_copy``
    does not re-run validators.
    """
    global _overrides, _effective
    _overrides = dict(values)
    _effective = None


def runtime_overrides() -> dict[str, Any]:
    """The currently applied DB-backed override layer (read-only copy)."""
    return dict(_overrides)


def _clear_settings_cache() -> None:
    """Drop both layers. Used by tests that mutate the environment."""
    global _overrides, _effective
    get_base_settings.cache_clear()
    _overrides = {}
    _effective = None


# Preserved so the many `get_settings.cache_clear()` calls in the test suite
# keep working now that get_settings is a merge rather than an lru_cache.
get_settings.cache_clear = _clear_settings_cache  # type: ignore[attr-defined]
