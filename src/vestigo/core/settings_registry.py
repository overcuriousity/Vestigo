"""Declarative catalog of every :class:`~vestigo.core.config.Settings` field.

This is the single source of truth for what the admin console can show and
edit. Each :class:`SettingSpec` carries only the metadata that *cannot* be
derived from the pydantic model — group, label, help text, and the policy
flags (``env_only``, ``secret``, ``restart_required``, ``subsystem``). The
value type and its constraints are read back off the model field itself
(:func:`field_kind`, :func:`field_constraints`) so a changed default or bound
can never drift from what the UI renders.

``tests/test_settings_api.py`` asserts the registry covers every
``Settings`` field: adding a setting without a spec fails the suite, which is
the mechanism that keeps "every setting is reachable from the web UI" true
over time.

Three policy flags decide how a field behaves in the console:

``env_only``
    Bootstrap or host-level configuration that cannot live in the database it
    would have to be read from (``postgres_url``), or that is consumed before
    the DB layer exists (``environment``, ``log_level``, the admin seed) or
    that names a filesystem path validated and created at startup. Shown
    read-only, never persisted.
``restart_required``
    Editable and persisted, but a running process keeps using the old value
    because the consumer is constructed once at startup (the ClickHouse and
    Qdrant clients). The UI says so next to the field.
``secret``
    Never returned by the API — the payload carries ``<field>_set: bool``
    instead. Storage follows the same contract ``agent_api_key`` already has:
    plaintext at rest, acceptable only when Postgres itself is trusted, and
    refused entirely under ``VESTIGO_SECRETS_MODE=env-only``.

Fields marked ``managed_by="agent"`` are persisted through the older, purpose-
built ``agent_settings`` row (``agent/config.py``) instead of ``app_settings``;
they are listed here for coverage and for the console's cross-link, but the
generic PUT refuses them.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from vestigo.core.config import Settings

#: Value kinds the admin UI knows how to render.
Kind = Literal["bool", "int", "float", "str", "secret", "choice", "json"]


@dataclass(frozen=True)
class SettingSpec:
    """UI/policy metadata for one ``Settings`` field."""

    field: str
    group: str
    label: str
    help: str
    env_only: bool = False
    secret: bool = False
    restart_required: bool = False
    #: Optional subsystem this field configures; a subsystem with no usable
    #: configuration is hidden from the analyst UI and the agent's tool list
    #: (see ``core/capabilities.py``).
    subsystem: str | None = None
    #: Non-None when another admin surface owns persistence for this field.
    managed_by: str | None = None
    #: Choice values for enum-ish string fields the pydantic pattern can't
    #: express as a machine-readable list.
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SettingGroup:
    """A titled section of the admin settings page."""

    key: str
    label: str
    description: str


GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup("general", "General", "Instance-wide behaviour and offline policy."),
    SettingGroup("auth", "Authentication", "Sessions, login backoff, and the audit trail."),
    SettingGroup("oidc", "Single sign-on (OIDC)", "Optional external identity provider."),
    SettingGroup(
        "stores", "Backing services", "Connections to ClickHouse, Qdrant, and PostgreSQL."
    ),
    SettingGroup("embeddings", "Embeddings", "Vector embedding model or remote endpoint."),
    SettingGroup("ingestion", "Ingestion", "Batch sizes and upload limits."),
    SettingGroup("detectors", "Anomaly detectors", "Thresholds and effect-size floors."),
    SettingGroup("scans", "Scan guardrails", "Memory, threads, and concurrency for heavy scans."),
    SettingGroup("enrichers", "Enrichment", "Enricher assets and scan batching."),
    SettingGroup("sigma", "Sigma rules", "Global ruleset directory and hit persistence."),
    SettingGroup("stories", "Stories", "Report export and block size ceilings."),
    SettingGroup("transfer", "Case transfer", "Export/import archive limits."),
    SettingGroup("agent", "AI agent", "LLM endpoint and tool policy."),
    SettingGroup(
        "converters",
        "Generated converters",
        "Let the configured model write converter scripts for plain-text logs.",
    ),
    SettingGroup("onboarding", "Onboarding", "What new users find the first time they log in."),
)

_SPECS: tuple[SettingSpec, ...] = (
    # ── General ──────────────────────────────────────────────────────────
    SettingSpec(
        "environment",
        "general",
        "Environment",
        "Deployment label (development/production). Read before the database layer exists.",
        env_only=True,
    ),
    SettingSpec(
        "log_level",
        "general",
        "Log level",
        "Root logger level. Applied when the process starts.",
        env_only=True,
    ),
    SettingSpec(
        "allow_online",
        "general",
        "Allow outbound network access",
        "Off by default (airgapped-first). Controls model-weight downloads and other "
        "unconfigured outbound calls. Operator-configured endpoints (OIDC, the LLM, a "
        "remote embeddings API) are deliberately independent of this switch.",
    ),
    # ── Authentication ───────────────────────────────────────────────────
    SettingSpec(
        "admin_username",
        "auth",
        "Bootstrap admin username",
        "Seeds the first administrator when no users exist. Consumed once, at startup.",
        env_only=True,
    ),
    SettingSpec(
        "admin_password",
        "auth",
        "Bootstrap admin password",
        "One-time seed password; the account must rotate it on first login.",
        env_only=True,
        secret=True,
    ),
    SettingSpec(
        "session_ttl_hours",
        "auth",
        "Session lifetime (hours)",
        "How long a session cookie stays valid. Applies to sessions created after the change.",
    ),
    SettingSpec("auth_cookie_name", "auth", "Session cookie name", "Name of the session cookie."),
    SettingSpec(
        "auth_cookie_secure",
        "auth",
        "Secure cookie flag",
        "Send the session cookie only over HTTPS. Enable behind a TLS reverse proxy.",
    ),
    SettingSpec(
        "auth_cookie_samesite",
        "auth",
        "Cookie SameSite policy",
        "SameSite attribute on the session cookie.",
        choices=("lax", "strict", "none"),
    ),
    SettingSpec(
        "audit_enabled",
        "auth",
        "Audit trail",
        "Record every authenticated mutation in the audit log. Forensic deployments keep this on.",
    ),
    SettingSpec(
        "login_backoff_threshold",
        "auth",
        "Login backoff threshold",
        "Consecutive failures per (username, client IP) before attempts are rejected with 429.",
    ),
    SettingSpec(
        "login_backoff_base_seconds",
        "auth",
        "Login backoff base (seconds)",
        "First lockout duration; doubles per additional failure past the threshold.",
    ),
    SettingSpec(
        "login_backoff_max_seconds",
        "auth",
        "Login backoff cap (seconds)",
        "Ceiling on the exponential lockout.",
    ),
    # ── OIDC ─────────────────────────────────────────────────────────────
    SettingSpec(
        "oidc_enabled",
        "oidc",
        "Enable OIDC single sign-on",
        "Off hides the SSO button on the login page entirely.",
        subsystem="oidc",
        restart_required=False,
    ),
    SettingSpec(
        "oidc_issuer",
        "oidc",
        "Issuer URL",
        "OIDC discovery base URL, e.g. https://auth.example.org/application/o/vestigo/",
        subsystem="oidc",
    ),
    SettingSpec("oidc_client_id", "oidc", "Client ID", "Client identifier.", subsystem="oidc"),
    SettingSpec(
        "oidc_client_secret",
        "oidc",
        "Client secret",
        "Client secret issued by the identity provider.",
        secret=True,
        subsystem="oidc",
    ),
    SettingSpec(
        "oidc_scopes",
        "oidc",
        "Scopes",
        "Space-separated scope list requested at authorization.",
        subsystem="oidc",
    ),
    SettingSpec(
        "oidc_redirect_url",
        "oidc",
        "Redirect URL",
        "Absolute callback URL registered with the provider.",
        subsystem="oidc",
    ),
    # ── Backing services ─────────────────────────────────────────────────
    SettingSpec(
        "postgres_url",
        "stores",
        "PostgreSQL DSN",
        "Metadata store connection. Environment-only by necessity — it is the database "
        "these settings are read from.",
        env_only=True,
        secret=True,
    ),
    SettingSpec(
        "clickhouse_url",
        "stores",
        "ClickHouse URL",
        "Event store HTTP endpoint.",
        restart_required=True,
    ),
    SettingSpec(
        "clickhouse_database",
        "stores",
        "ClickHouse database",
        "Database events are written to.",
        restart_required=True,
    ),
    SettingSpec(
        "clickhouse_username", "stores", "ClickHouse user", "User name.", restart_required=True
    ),
    SettingSpec(
        "clickhouse_password",
        "stores",
        "ClickHouse password",
        "Password for the ClickHouse user.",
        secret=True,
        restart_required=True,
    ),
    SettingSpec(
        "qdrant_url",
        "stores",
        "Qdrant URL",
        "Vector store endpoint. Ignored entirely when the (environment-only) Qdrant local "
        "path is set, which is how an embedded on-disk Qdrant is selected — clearing this "
        "field here restores the default endpoint rather than unsetting it.",
        restart_required=True,
    ),
    SettingSpec(
        "qdrant_path",
        "stores",
        "Qdrant local path",
        "Embedded Qdrant storage directory, used when no URL is set.",
        env_only=True,
    ),
    SettingSpec(
        "qdrant_api_key",
        "stores",
        "Qdrant API key",
        "Optional API key for a secured Qdrant.",
        secret=True,
        restart_required=True,
    ),
    SettingSpec(
        "qdrant_collection_prefix",
        "stores",
        "Qdrant collection prefix",
        "Prefix for the per-(case, embedding config) collections.",
        restart_required=True,
    ),
    SettingSpec(
        "source_retention_path",
        "stores",
        "Source retention directory",
        "Where the immutable copy of every ingested file is kept.",
        env_only=True,
    ),
    SettingSpec(
        "transfer_temp_path",
        "stores",
        "Transfer staging directory",
        "Where in-flight case export archives are written (created 0700 at startup).",
        env_only=True,
    ),
    SettingSpec(
        "enricher_data_path",
        "stores",
        "Enricher asset directory",
        "Where admin-uploaded enricher assets (e.g. GeoLite2) are stored.",
        env_only=True,
    ),
    # ── Embeddings ───────────────────────────────────────────────────────
    SettingSpec(
        "embedding_model",
        "embeddings",
        "Embedding model",
        "Local sentence-transformers model name, or the model id sent to the remote endpoint.",
        subsystem="embeddings",
    ),
    SettingSpec(
        "embedding_device",
        "embeddings",
        "Device",
        "Torch device for the local model (cpu, cuda, cuda:0, …). Ignored for remote endpoints.",
        subsystem="embeddings",
    ),
    SettingSpec(
        "embedding_batch_size",
        "embeddings",
        "Batch size",
        "Events per embedding call. Memory-bound by the model — keep it well below the "
        "ingestion batch size.",
        subsystem="embeddings",
    ),
    SettingSpec(
        "embedding_api_base_url",
        "embeddings",
        "Remote endpoint URL",
        "OpenAI-compatible base URL. Set it to embed via a remote service instead of a "
        "local model — no local ML dependencies needed.",
        subsystem="embeddings",
    ),
    SettingSpec(
        "embedding_api_key",
        "embeddings",
        "Remote endpoint API key",
        "Bearer token for the remote embeddings endpoint.",
        secret=True,
        subsystem="embeddings",
    ),
    # ── Ingestion ────────────────────────────────────────────────────────
    SettingSpec(
        "ingest_batch_size",
        "ingestion",
        "Ingest batch size",
        "Events per ClickHouse insert. Larger batches amortize latency; ~2-4 KB of memory "
        "per queued event.",
    ),
    SettingSpec(
        "max_upload_bytes",
        "ingestion",
        "Maximum upload size (bytes)",
        "Ceiling on a single source upload. 0 disables the limit.",
    ),
    # ── Detectors ────────────────────────────────────────────────────────
    SettingSpec(
        "stat_rarity_floor",
        "detectors",
        "Value-novelty rarity floor",
        "Occurrence count at or below which a value is flagged as rare.",
    ),
    SettingSpec(
        "stat_charset_rarity_floor",
        "detectors",
        "Charset rarity floor",
        "Distinct values a character may appear in before it stops counting as rare.",
    ),
    SettingSpec(
        "stat_z_threshold",
        "detectors",
        "Frequency z-score threshold",
        "Z-score at which a frequency bucket is flagged.",
    ),
    SettingSpec(
        "stat_frequency_buckets",
        "detectors",
        "Frequency buckets",
        "Number of time buckets the frequency detector splits a window into.",
    ),
    SettingSpec(
        "stat_per_field_limit",
        "detectors",
        "Per-field result limit",
        "Default cap on rare values reported per field.",
    ),
    SettingSpec(
        "stat_order_min_skew",
        "detectors",
        "Timestamp-order minimum skew (seconds)",
        "Smallest backwards jump that counts as an order violation. 0 is AMiner-strict.",
    ),
    SettingSpec(
        "stat_shift_fdr_q",
        "detectors",
        "Proportion-shift FDR q",
        "Benjamini-Hochberg false-discovery ceiling for the proportion-shift detector.",
    ),
    SettingSpec(
        "stat_shift_min_ratio",
        "detectors",
        "Proportion-shift effect floor",
        "Minimum share ratio between suspect and baseline before a finding is reported.",
    ),
    SettingSpec(
        "stat_shift_max_candidates_per_field",
        "detectors",
        "Proportion-shift candidate cap",
        "Values fetched per field, highest volume first. Hitting it carries a warning.",
    ),
    SettingSpec(
        "stat_interval_fdr_q",
        "detectors",
        "Interval FDR q",
        "False-discovery ceiling shared by both cadence directions.",
    ),
    SettingSpec(
        "stat_interval_min_rate_ratio",
        "detectors",
        "Cadence-break effect floor",
        "Minimum arrival-rate ratio between suspect and baseline.",
    ),
    SettingSpec(
        "stat_interval_min_baseline_intervals",
        "detectors",
        "Minimum baseline intervals",
        "Inter-arrival intervals needed before a value's cadence counts as learned.",
    ),
    SettingSpec(
        "stat_interval_cv_regular_max",
        "detectors",
        "Regular-cadence CV ceiling",
        "Baseline delta-CV at or below which a value is 'regular' and testable for breaks.",
    ),
    SettingSpec(
        "stat_interval_cv_irregular_min",
        "detectors",
        "Irregular-cadence CV floor",
        "Baseline delta-CV at or above which a value is eligible for beaconing tests.",
    ),
    SettingSpec(
        "stat_interval_beacon_min_intervals",
        "detectors",
        "Beaconing minimum intervals",
        "Suspect-window intervals needed before the Greenwood statistic is trusted.",
    ),
    SettingSpec(
        "stat_interval_beacon_cv_max",
        "detectors",
        "Beaconing CV ceiling",
        "Suspect-window delta-CV at or below which arrivals read as beaconing.",
    ),
    SettingSpec(
        "stat_interval_beacon_min_span",
        "detectors",
        "Beaconing span floor",
        "Fraction of the suspect window a value's activity must cover.",
    ),
    SettingSpec(
        "stat_interval_max_candidates_per_field",
        "detectors",
        "Interval candidate cap",
        "Values fetched per field for the interval scan.",
    ),
    SettingSpec(
        "stat_drift_fdr_q",
        "detectors",
        "Distribution-drift FDR q",
        "False-discovery ceiling shared by the KS and G-test branches.",
    ),
    SettingSpec(
        "stat_drift_min_ks_d",
        "detectors",
        "Drift KS effect floor",
        "Minimum KS D statistic (largest CDF gap) for a numeric drift finding.",
    ),
    SettingSpec(
        "stat_drift_min_tvd",
        "detectors",
        "Drift TVD effect floor",
        "Minimum total-variation distance for a categorical drift finding.",
    ),
    SettingSpec(
        "stat_drift_min_samples",
        "detectors",
        "Drift minimum samples",
        "Field-bearing events required on each side of a drift test.",
    ),
    SettingSpec(
        "stat_sequence_ngram",
        "detectors",
        "Sequence n-gram length",
        "Events per n-gram for the sequence-novelty detector.",
    ),
    SettingSpec(
        "stat_sequence_max_candidates",
        "detectors",
        "Sequence candidate cap",
        "Novel n-grams fetched per run, rarest first.",
    ),
    SettingSpec(
        "stat_motif_min_support",
        "detectors",
        "Motif minimum support",
        "Occurrences before an n-gram counts as a recurring motif.",
    ),
    SettingSpec(
        "stat_motif_max_candidates",
        "detectors",
        "Motif candidate cap",
        "Candidate motifs fetched per source, highest support first.",
    ),
    SettingSpec(
        "stat_motif_cadence_top_k",
        "detectors",
        "Motif cadence top-K",
        "Merged candidates that get the second (cadence) pass.",
    ),
    SettingSpec(
        "analysis_gate_min_numeric_ratio",
        "detectors",
        "Gate: numeric-field ratio",
        "Share of a field's sampled values that must parse as numbers before the numeric-range method is offered automatically. Gated methods stay runnable on request.",
    ),
    SettingSpec(
        "analysis_gate_max_enum_distinct",
        "detectors",
        "Gate: enum-like distinct ceiling",
        "A field with at most this many distinct values counts as enum-like: every value is drawn from the alphabet learned from those same values, so charset novelty cannot fire. Entropy is not gated on this — its band is a comparison one enum literal can still sit outside.",
    ),
    SettingSpec(
        "analysis_gate_min_series_distinct",
        "detectors",
        "Gate: minimum series values",
        "Distinct values the series field needs before two n-grams can differ at all. One value yields a single repeated n-gram; two already yield 2^n.",
    ),
    SettingSpec(
        "analysis_gate_min_frequency_buckets",
        "detectors",
        "Gate: minimum frequency span (seconds)",
        "Seconds of span a timeline must cover before a count spike is distinguishable from the bucket holding it. Compare with Frequency buckets, which is how many buckets that span is split into.",
    ),
    SettingSpec(
        "analysis_gate_min_interval_periods",
        "detectors",
        "Gate: minimum cadence periods",
        "Repeats a series value needs before an inter-arrival cadence can be fitted.",
    ),
    SettingSpec(
        "analysis_cache_max_rows_per_case",
        "detectors",
        "Analysis cache rows per case",
        "Cached method results retained per case, least-recently-computed evicted first. Every row is derived data — eviction only costs a rescan.",
    ),
    SettingSpec(
        "viz_baseline_cache_entries",
        "detectors",
        "Visualize baseline cache entries",
        "Memoized baseline layers for compare renders. 0 disables the cache.",
    ),
    # ── Scan guardrails ──────────────────────────────────────────────────
    # The SETTINGS clause is built per query (db/_scan.py::heavy_scan_settings),
    # so an edit to any value it interpolates lands on the next scan. Only
    # `stat_scan_concurrency` still needs a restart: it sizes the admission
    # semaphore, which every scan module imports by value.
    SettingSpec(
        "stat_scan_max_threads",
        "scans",
        "Max threads per scan",
        "ClickHouse max_threads for heavy detector/inventory scans. 0 = auto: an even share "
        "of the cores ClickHouse reports for itself (cores / concurrent-scans, floor 2), so "
        "a full admission gate saturates the box rather than oversubscribing it. Pin a value "
        "only to override that. /api/health reports what resolved and whether it was "
        "detected or pinned.",
    ),
    SettingSpec(
        "stat_scan_external_group_by_bytes",
        "scans",
        "GROUP BY spill threshold (bytes)",
        "Bytes after which a heavy GROUP BY spills to disk.",
    ),
    SettingSpec(
        "stat_scan_external_sort_bytes",
        "scans",
        "ORDER BY spill threshold (bytes)",
        "Bytes after which a plain sort spills to disk (window sorts cannot spill).",
    ),
    SettingSpec(
        "stat_scan_max_memory_bytes",
        "scans",
        "Scan memory budget (bytes)",
        "Total budget shared across concurrent scans. 0 = auto: a fraction (below) of the "
        "ceiling ClickHouse reports for itself, which the app reads from the server at "
        "startup. Pin a byte value only to override that. If ClickHouse reports no ceiling "
        "the app falls back to its own container's view of RAM, which in a full-docker "
        "stack is the whole host and assumes ClickHouse owns it — set "
        "max_server_memory_usage on the server instead of pinning around it.",
    ),
    SettingSpec(
        "stat_scan_memory_ratio",
        "scans",
        "Auto-budget memory ratio",
        "Fraction of what is left of ClickHouse's memory ceiling *after* its own caches "
        "(mark, index-mark, primary-index, uncompressed) that the automatic budget uses. "
        "The remainder is headroom for the one thing no per-query cap can bound: background "
        "merges, plus allocator slack. /api/health reports the cache figure that went into "
        "the subtraction.",
    ),
    SettingSpec(
        "enrichment_apply_merge_wait_seconds",
        "scans",
        "Merge wait after an enrichment apply (seconds)",
        "How long the enrichment partition rewrite keeps its scan slot after the swap, "
        "waiting out the merges the swap queued. Merges are the one memory consumer a "
        "per-query cap cannot bound, so releasing the slot at the swap admits the next "
        "sweep straight into them. 0 skips the wait — right when ClickHouse has a "
        "max_server_memory_usage of its own, which bounds merges directly.",
    ),
    SettingSpec(
        "export_scan_queue_wait_seconds",
        "scans",
        "Export queue wait (seconds)",
        "How long a value-inventory export waits for the single streamed-export slot before "
        "the request is refused with 503. That slot is held for the whole download, which "
        "the analyst's browser paces, so the wait is bounded rather than open-ended: an "
        "unbounded one lets a single backgrounded download block every other export in the "
        "process. 0 refuses immediately when an export is already running.",
    ),
    SettingSpec(
        "stat_scan_concurrency",
        "scans",
        "Concurrent heavy scans",
        "Scans allowed against ClickHouse at once; the rest queue. Guards against an "
        "OOM-kill of the server. The enrichment partition rewrite takes a slot too.",
        restart_required=True,
    ),
    SettingSpec(
        "enrichment_apply_insert_block_bytes",
        "scans",
        "Enrichment apply insert block size (bytes)",
        "Squash floor for the enrichment partition rewrite's INSERT: rows accumulate until "
        "a block reaches at least this size before a part is written. Lower it to trade more "
        "(smaller) parts and more merge work for less insert-time memory. Unlike the scan "
        "settings this one is not restart-required — it is interpolated per call, not frozen "
        "into a module-level SETTINGS clause at import.",
        restart_required=False,
    ),
    # ── Enrichment ───────────────────────────────────────────────────────
    SettingSpec(
        "enrichment_batch_size",
        "enrichers",
        "Enrichment batch size",
        "Events read per ClickHouse round-trip during an enrichment scan. Round-trip bound, "
        "so this pages far larger than the embedding batch.",
        subsystem="enrichers",
    ),
    # ── Sigma ────────────────────────────────────────────────────────────
    SettingSpec(
        "sigma_rules_path",
        "sigma",
        "Global ruleset directory",
        "Directory scanned for *.yml/*.yaml at run time (e.g. a vendored SigmaHQ clone). "
        "Empty disables the global set; per-case uploads still work.",
        subsystem="sigma",
    ),
    SettingSpec(
        "sigma_annotation_batch_size",
        "sigma",
        "Hit persistence batch size",
        "Rows per bulk annotation write while a Sigma run persists hits.",
        subsystem="sigma",
    ),
    # ── Stories ──────────────────────────────────────────────────────────
    SettingSpec(
        "story_export_max_blocks",
        "stories",
        "Max blocks per export",
        "Bounds how much querying one synchronous export can trigger.",
        subsystem="stories",
    ),
    SettingSpec(
        "story_export_max_snapshot_bytes",
        "stories",
        "Max snapshot size (bytes)",
        "Ceiling on the frozen-blocks JSON stored with an export.",
        subsystem="stories",
    ),
    SettingSpec(
        "story_max_artifact_bytes",
        "stories",
        "Max artifact size (bytes)",
        "Ceiling on the uploaded standalone HTML report.",
        subsystem="stories",
    ),
    SettingSpec(
        "story_max_markdown_bytes",
        "stories",
        "Max markdown block size (bytes)",
        "Ceiling on one markdown block's text — it is embedded verbatim into every later snapshot.",
        subsystem="stories",
    ),
    # ── Case transfer ────────────────────────────────────────────────────
    SettingSpec(
        "transfer_enabled",
        "transfer",
        "Enable case export/import",
        "Off hides case transfer from the UI entirely and refuses its endpoints — for "
        "deployments where a whole-case archive leaving the box is a policy problem.",
        subsystem="transfer",
    ),
    SettingSpec(
        "transfer_max_expanded_bytes",
        "transfer",
        "Max expanded archive size (bytes)",
        "Ceiling on an imported archive's uncompressed size. 0 disables; a large expansion "
        "ratio means a decompression bomb, not a big case.",
        subsystem="transfer",
    ),
    SettingSpec(
        "transfer_max_metadata_bytes",
        "transfer",
        "Max metadata member size (bytes)",
        "Ceiling on any single postgres/* member of an imported archive.",
        subsystem="transfer",
    ),
    SettingSpec(
        "transfer_max_concurrent",
        "transfer",
        "Concurrent transfer jobs",
        "Export/import jobs allowed at once across the instance. 0 removes the cap — it "
        "does not disable the feature (use the switch above for that).",
        subsystem="transfer",
    ),
    # ── AI agent ─────────────────────────────────────────────────────────
    SettingSpec(
        "agent_secret_mode",
        "agent",
        "Agent key storage mode",
        "'db' lets admins store the LLM API key in Postgres; 'env-only' refuses storage and "
        "ignores any previously stored key.",
        choices=("db", "env-only"),
        subsystem="agent",
    ),
    SettingSpec(
        "agent_probe_ttl_seconds",
        "agent",
        "Availability probe TTL (seconds)",
        "How long an endpoint availability result is cached before re-probing.",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_request_timeout_seconds",
        "agent",
        "Model request timeout (seconds)",
        "Wall-clock ceiling for one model request inside an agent turn. Raise it for a "
        "slow local endpoint; the stranded-turn sweep scales with it automatically.",
        subsystem="agent",
    ),
    SettingSpec(
        "column_advisor_timeout_seconds",
        "agent",
        "Column suggestion timeout (seconds)",
        "Ceiling for the one-shot 'suggest columns with AI' call, probe and all. On "
        "expiry the timeline keeps the locally scored columns.",
        subsystem="agent",
    ),
    SettingSpec(
        "mcp_enabled",
        "agent",
        "External MCP endpoint",
        "Serve the scoped tool server over streamable HTTP at /mcp, authenticated by "
        "per-timeline tokens. Independent of the in-app agent.",
        subsystem="mcp",
    ),
    SettingSpec(
        "secrets_mode",
        "general",
        "Secret storage mode",
        "'db' lets admins store secrets (passwords, API keys) in Postgres in plaintext; "
        "'env-only' refuses storage instance-wide and ignores anything already stored.",
        choices=("db", "env-only"),
        env_only=True,
    ),
    # Persisted by the dedicated agent settings row (docs/AGENT.md); listed
    # here so coverage stays exhaustive and the console can cross-link.
    SettingSpec(
        "agent_model", "agent", "Model", "Model id.", managed_by="agent", subsystem="agent"
    ),
    SettingSpec(
        "agent_provider",
        "agent",
        "Wire protocol",
        "openai or anthropic.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_api_base_url",
        "agent",
        "Endpoint URL",
        "LLM endpoint base URL.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_api_key",
        "agent",
        "API key",
        "LLM endpoint credential.",
        secret=True,
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_user_agent",
        "agent",
        "User-Agent override",
        "Some subscription endpoints gate on it.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_extra_headers",
        "agent",
        "Extra headers",
        "Additional HTTP headers as a JSON object.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_max_turns",
        "agent",
        "Max turns",
        "Model round-trips per user message.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_reasoning_effort",
        "agent",
        "Reasoning effort",
        "Passed through when the provider supports it.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_context_window",
        "agent",
        "Context window",
        "Model context window in tokens.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_tool_fidelity",
        "agent",
        "Tool result fidelity",
        "How much of an example record tool results carry.",
        managed_by="agent",
        subsystem="agent",
    ),
    SettingSpec(
        "agent_disabled_tools",
        "agent",
        "Disabled tools",
        "Admin hard-deny list applied to the in-app agent and /mcp alike.",
        managed_by="agent",
        subsystem="agent",
    ),
    # ── Generated converters ─────────────────────────────────────────────
    SettingSpec(
        "converter_generation_enabled",
        "converters",
        "Generate converters with the AI model",
        "When on and an agent endpoint is configured, the upload dialog can send a sample "
        "of a plain-text log to the model, run the script it writes in a guarded "
        "subprocess on this host, and ingest the result. Off by default because it "
        "executes model-written code here (docs/DEPLOYMENT.md).",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_max_attempts",
        "converters",
        "Generation attempts",
        "How many times the model may rewrite the script after a failed sample run.",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_sample_bytes",
        "converters",
        "Sample size sent to the model (bytes)",
        "Head, middle and tail of the raw file, up to this many bytes, are the only "
        "evidence that leaves this host. The excerpt is condensed to distinct line "
        "shapes first, so a repetitive log sends far less than this.",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_generation_timeout_seconds",
        "converters",
        "Generation timeout (seconds)",
        "Ceiling for one generation or repair round, including the availability probe. "
        "A model too slow to write a script within it fails every attempt — raise it "
        "for a large local model rather than lowering the attempt count.",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_run_timeout_seconds",
        "converters",
        "Conversion timeout (seconds)",
        "Wall-clock ceiling for one full-file conversion run.",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_run_memory_mb",
        "converters",
        "Conversion memory ceiling (MB)",
        "Address-space limit for the converter subprocess. pyarrow needs at least 2048.",
        subsystem="converter_generation",
    ),
    SettingSpec(
        "converter_run_output_mb",
        "converters",
        "Conversion output ceiling (MB)",
        "Largest Parquet file a converter run may write.",
        subsystem="converter_generation",
    ),
    # ── Onboarding ───────────────────────────────────────────────────────
    SettingSpec(
        "demo_case_enabled",
        "onboarding",
        "Seed a demo case for new users",
        "Imports a fabricated example investigation into each user's case list on their "
        "first login. Seeded once per user; deleting it is final unless they restore it "
        "explicitly. Turning this off stops future seeding and leaves existing copies alone.",
        subsystem="demo_case",
    ),
    SettingSpec(
        "demo_max_concurrent",
        "onboarding",
        "Concurrent demo-case builds",
        "Demo cases prepared at once across the instance. The build is CPU-bound, so each "
        "one competes with the rest of the app; a burst of first logins queues behind this "
        "and retries at the next login. 0 removes the cap — it does not disable seeding "
        "(use the switch above for that).",
        subsystem="demo_case",
    ),
)

SPECS_BY_FIELD: dict[str, SettingSpec] = {s.field: s for s in _SPECS}


def all_specs() -> tuple[SettingSpec, ...]:
    """Every registered spec, in declaration (i.e. display) order."""
    return _SPECS


def editable_fields() -> frozenset[str]:
    """Fields the generic settings API may persist to ``app_settings``."""
    return frozenset(s.field for s in _SPECS if not s.env_only and s.managed_by is None)


def secret_fields() -> frozenset[str]:
    """Fields whose value is never returned by the API."""
    return frozenset(s.field for s in _SPECS if s.secret)


@lru_cache
def field_kind(field: str) -> Kind:
    """Render kind for one field, derived from its annotation plus spec flags."""
    spec = SPECS_BY_FIELD.get(field)
    if spec is not None and spec.secret:
        return "secret"
    if spec is not None and spec.choices:
        return "choice"
    annotation = Settings.model_fields[field].annotation
    for candidate in _unwrap_optional(annotation):
        if candidate is bool:
            return "bool"
        if candidate is int:
            return "int"
        if candidate is float:
            return "float"
        if candidate is str:
            return "str"
        if candidate in (dict, list) or typing.get_origin(candidate) in (dict, list):
            return "json"
    return "str"


@lru_cache
def is_nullable(field: str) -> bool:
    """Whether ``None`` is a value this field accepts.

    Read off the annotation rather than restated in the spec, for the same
    reason as :func:`field_constraints`. The console needs it because an empty
    text box is ambiguous: for ``sigma_rules_path`` (a plain ``str``) empty is a
    meaningful value that disables the global ruleset, while for an optional
    field it means "unset", and storing ``""`` there would leave the setting
    reading as customized forever.
    """
    annotation = Settings.model_fields[field].annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return annotation is type(None)


def _unwrap_optional(annotation: Any) -> tuple[Any, ...]:
    """Return the non-``None`` members of a possibly-optional annotation."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return tuple(a for a in typing.get_args(annotation) if a is not type(None))
    return (annotation,)


def field_constraints(field: str) -> dict[str, Any]:
    """Numeric/pattern constraints declared on the pydantic field, if any.

    Read off the model rather than restated in the spec so a bound tightened
    in ``config.py`` reaches the UI without a second edit.
    """
    out: dict[str, Any] = {}
    for meta in Settings.model_fields[field].metadata:
        for attr, key in (
            ("ge", "ge"),
            ("gt", "gt"),
            ("le", "le"),
            ("lt", "lt"),
            ("pattern", "pattern"),
        ):
            value = getattr(meta, attr, None)
            if value is not None:
                out[key] = value
    return out


def env_var_name(field: str) -> str:
    """The environment variable that pins one field."""
    return f"VESTIGO_{field.upper()}"


def default_value(field: str) -> Any:
    """The hardcoded default for one field (what a cleared override falls back to)."""
    return Settings.model_fields[field].get_default(call_default_factory=True)
