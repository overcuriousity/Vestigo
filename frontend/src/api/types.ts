/** Typed API contract for TraceSignal. Mirrors the FastAPI backend models. */

export interface Case {
  id: string;
  name: string;
  description: string | null;
  /** Creator's user id. */
  owner_id: string | null;
  /** Investigation team this case belongs to, or null for a personal case. */
  team_id: string | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Auth / users / teams / audit
// ---------------------------------------------------------------------------

export type AuthProvider = "local" | "oidc";

export interface TeamMembershipSummary {
  id: string;
  name: string;
  role: "member" | "manager";
}

export interface User {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  is_admin: boolean;
  is_active: boolean;
  must_change_password: boolean;
  auth_provider: AuthProvider;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
  /** Only present on /auth/me and /auth/me/password responses. */
  teams?: TeamMembershipSummary[];
}

export interface Team {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export type TeamRole = "member" | "manager";

export interface TeamMember extends User {
  role: TeamRole;
}

export interface AuditEntry {
  id: string;
  timestamp: string;
  user_id: string | null;
  username: string | null;
  action: string;
  method: string | null;
  path: string | null;
  route: string | null;
  case_id: string | null;
  target_type: string | null;
  target_id: string | null;
  status_code: number | null;
  ip: string | null;
  user_agent: string | null;
  detail: Record<string, unknown> | null;
}

/**
 * Per-artifact field selection stored on a source after the embedding wizard.
 * Shape: { version: 1, artifacts: { "<artifact>": ["message", "attr:user_agent"] } }
 */
export interface EmbeddingFieldConfig {
  version: 1;
  artifacts: Record<string, string[]>;
}

export interface Source {
  id: string;
  case_id: string;
  name: string;
  description: string | null;
  filename: string | null;
  file_hash: string;
  size_bytes: number;
  parser: string | null;
  parser_version: string | null;
  event_count: number;
  vector_count: number;
  /** Ingest lifecycle: "ingesting" sources are excluded from timeline queries until "ready". */
  status: "ingesting" | "ready";
  embedding_model: string | null;
  /** Analyst-defined per-artifact field selection, null when not yet configured. */
  embedding_config: EmbeddingFieldConfig | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface Timeline {
  id: string;
  case_id: string;
  name: string;
  description: string | null;
  is_default: boolean;
  source_ids: string[];
  /** True when an embedding job has completed for this timeline. */
  is_embedded: boolean;
  /**
   * True when the current source set differs from the set that was
   * embedded — analysis may be incomplete.
   */
  is_stale: boolean;
  /** The analyst-defined field config used for the most recent embed. */
  embedding_config: EmbeddingFieldConfig | null;
  embedding_model: string | null;
  embedded_source_ids: string[] | null;
  embedded_at: string | null;
  /** Canonical field name -> ordered raw attribute keys (query-time merge). */
  field_mappings: Record<string, string[]> | null;
  created_at: string;
  updated_at: string;
}

/** Per-source presence of one raw attribute key (timeline wizard). */
export interface FieldCoverageSource {
  source_id: string;
  count: number;
  samples: string[];
}

export interface FieldCoverageEntry {
  key: string;
  sources: FieldCoverageSource[];
}

export interface FieldCoverageResponse {
  fields: FieldCoverageEntry[];
}

export interface Event {
  event_id: string;
  case_id: string;
  source_id: string;
  source_file: string;
  byte_offset: number;
  line_number: number | null;
  content_hash: string;
  file_hash: string;
  parser_name: string;
  parser_version: string;
  ingest_time: string;
  message: string;
  timestamp: string | null;
  timestamp_desc: string | null;
  artifact: string | null;
  artifact_long: string | null;
  display_name: string | null;
  /** Parser-derived tags (ClickHouse). Different from annotation tags. */
  tags: string[];
  attributes: Record<string, string>;
  embedding_model: string | null;
  embedding_config_hash: string | null;
  vector_id: string | null;
}

export interface EventPage {
  /** Only computed on the initial, uncursored fetch — null on cursor pages. */
  total: number | null;
  offset: number;
  limit: number;
  events: Event[];
  has_more_after: boolean;
  has_more_before: boolean;
  next_cursor: [string, string] | null;
  prev_cursor: [string, string] | null;
}

/** Keyset pagination cursor: "<iso-timestamp>,<event_id>". */
export interface EventCursor {
  after?: string;
  before?: string;
}

export interface View {
  id: string;
  case_id: string;
  name: string;
  query: string;
  filter: Record<string, unknown>;
  created_at: string;
}

export type AnnotationType = "comment" | "tag" | "anomaly" | "normal";
export type AnnotationOrigin = "user" | "system";

export interface Annotation {
  id: string;
  case_id: string;
  source_id: string;
  event_id: string;
  annotation_type: AnnotationType;
  content: string;
  origin: AnnotationOrigin;
  created_by: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
  /** True only for a system annotation created via the per-event "Persist" action. */
  pinned: boolean;
  /** Which detector produced this system annotation ("value_novelty" | "frequency"); null for human annotations. */
  detector: string | null;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed";
  progress:
    | {
        total: number;
        processed: number;
        /** Kalman-filtered throughput/ETA (bytes ingest jobs only; see
         * core/eta.py). Absent for embed jobs and before the second batch. */
        rate_bps?: number | null;
        rate_std_bps?: number | null;
        kalman_gain?: number | null;
        eta_s?: number | null;
        eta_sigma_s?: number | null;
      }
    | null;
  result: unknown;
  error: string | null;
}

export interface SimilarResult {
  event_id: string;
  score: number;
  event: Event;
}

export interface SimilarityResponse {
  status: "ok" | "not_embedded" | "vector_not_found";
  results: SimilarResult[];
}

// ---------------------------------------------------------------------------
// Statistical anomaly detection types
// ---------------------------------------------------------------------------

/** One rare / first-seen value finding from the value_novelty detector. */
export interface ValueNoveltyFinding {
  type: "value_novelty";
  field: string;
  value: string;
  count: number;
  /** -log(count/total) — higher is rarer. */
  score: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
}

/** One anomalous time window from the frequency detector. */
export interface FrequencyFinding {
  type: "frequency";
  series_field: string;
  series_value: string;
  window_start: string;
  window_end: string;
  observed: number;
  expected: number;
  z_score: number;
  /** |z_score| — used for ranking. */
  score: number;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
}

export type AnomalyFinding = ValueNoveltyFinding | FrequencyFinding;

export interface AnomaliesResponse {
  status: "ok" | "no_data" | "insufficient_data";
  /** "value_novelty" | "frequency" */
  detector: string;
  /** "self-baseline" | "temporal" | "z-score" | "temporal-z-score" */
  method: string;
  baseline_size: number;
  results: AnomalyFinding[];
  /** Effective |z| cutoff used by the frequency detector; null for value_novelty. */
  z_threshold: number | null;
  /**
   * ID of the persisted DetectorRun for this scan (null when `status` isn't
   * "ok", or when the request opted out via `persist=false`). Reference this
   * by `EventFilters.anomalyRunId` to filter the grid/histogram/export to
   * this scan's findings instead of re-uploading event IDs.
   */
  run_id: string | null;
}

/** One active finding fed to the histogram overlay / event grid highlighting. */
export interface AnomalyMarker {
  ts: string;
  /** Short "field=value" form — used for compact contexts (histogram flag hover). */
  label: string;
  /**
   * Full, human-readable explanation of the finding (field/value/count/score,
   * or window/observed/expected/z-score) — shown in the event detail panel so
   * an analyst can see *why* an event was flagged without re-opening the
   * Analysis panel. Falls back to `label` when a fuller explanation isn't
   * available.
   */
  detail: string;
  /** Representative event for this finding, when the detector supplied one. */
  eventId?: string | null;
  /** Source id of the representative event — required to persist this finding. */
  sourceId?: string | null;
  /** Which detector produced this finding — required to persist this finding. */
  detector: "value_novelty" | "frequency";
  /** Raw structured finding data — stored verbatim on the persisted annotation. */
  rawDetails: Record<string, unknown>;
  /** End of the anomalous window, for frequency findings — enables a range highlight. */
  windowEnd?: string | null;
}

export interface TagAnomaliesResponse extends AnomaliesResponse {
  tagged: number;
  /** Findings whose representative event couldn't be resolved and were skipped. */
  skipped_unresolved: number;
}

/** One field candidate returned by GET /anomalies/fields. */
export interface NoveltyFieldInfo {
  /** Field token, e.g. "artifact" or "attr:status_code". */
  token: string;
  /** Number of distinct non-empty values (uniqExact). */
  distinct: number;
  /** Fraction of events with a non-empty value (0–1). */
  coverage: number;
  /** "categorical" | "constant" | "identifier" | "sparse" */
  kind: string;
  /** True when the field is useful for novelty detection. */
  recommended: boolean;
}

export interface NoveltyFieldsResponse {
  fields: NoveltyFieldInfo[];
}

/** Per-field heuristic verdict from the wizard recommender. */
export interface FieldVerdict {
  /** "message" or "attr:<key>" */
  token: string;
  recommended: boolean;
  /**
   * "text" | "shared-cohesive" | "divergent" | "source-specific"
   * | "numeric" | "hash" | "guid" | "id" | "constant" | "empty"
   */
  kind: string;
  reason: string;
  /** How many of the timeline's sources contain this field. */
  present_in_sources: number;
  /**
   * Mean pairwise cosine between per-source value-centroids.
   * null when fewer than 2 sources have the field or encode is absent.
   */
  cohesion: number | null;
}

/** Timeline-level embedding substrate quality verdict. */
export interface CohesionSummary {
  /** "strong" | "moderate" | "weak" | "unavailable" */
  level: string;
  /** Mean cohesion across shared fields; null when unavailable. */
  mean_cohesion: number | null;
  /** Number of text-rich fields present in ≥2 sources. */
  shared_field_count: number;
  source_count: number;
  message: string;
}

/** Per-artifact field info returned by /embedding-fields */
export interface EmbeddingArtifactInfo {
  artifact: string;
  count: number;
  /** Fixed top-level fields available for embedding */
  top_level: string[];
  /** Dynamic attribute keys found for this artifact */
  attributes: string[];
  /** Recommended preselection (tokens like "message", "attr:user_agent") */
  recommended: string[];
  /** Per-field verdict explaining why each field was kept or dropped */
  field_analysis: FieldVerdict[];
  /** Groups of fields whose values embed close together (semantically related) */
  related_groups: string[][];
}

export interface EmbeddingFieldsResponse {
  artifacts: EmbeddingArtifactInfo[];
  /** Timeline-level cohesion summary. */
  cohesion: CohesionSummary;
}

export interface UploadResult {
  source_id: string;
  events_parsed: number;
  events_inserted: number;
  parser: string;
  duplicate?: boolean;
  /** Ingest lifecycle of source_id at response time — "ingesting" | "ready". */
  status?: string;
  /** Background ingestion job to poll for progress; null for duplicates. */
  job_id?: string | null;
}

export interface HealthResponse {
  status: "ok";
  version: string;
  oidc_enabled: boolean;
}

/** Non-default field-filter match modes; "exact" is implied by absence. */
export type FieldMatchMode = "wildcard" | "regex";

/** Filter params for the events query */
export interface EventFilters {
  q?: string;
  /**
   * Search mode for `q`. Absent = keyword (the default). "semantic" runs the
   * embedding-based search client-side and replaces `q` with result ids —
   * an explicit analyst choice, never inferred (forensic reproducibility:
   * a shared URL or saved view must reproduce the exact search semantics).
   */
  qMode?: "semantic";
  /** Treat `q` as an RE2 regex server-side (keyword mode only). */
  qRegex?: boolean;
  artifact?: string;
  /** Multi-select artifact filter (OR'd); distinct from the single-value `artifact`. */
  artifacts?: string[];
  sourceId?: string;
  tag?: string;
  excludeTag?: string;
  /**
   * Unified tag filter (OR'd) — matches either a user annotation tag or a
   * parser-derived Event.tags value with this exact content.
   */
  tagsInclude?: string[];
  /** Unified tag values to exclude — an event is dropped if it has any of these. */
  tagsExclude?: string[];
  start?: string;
  end?: string;
  /** key=value field equality filters */
  filters?: Record<string, string>;
  /** key=[values] field exclusion filters — multiple values per field are OR'd (NOT IN) */
  exclusions?: Record<string, string[]>;
  /**
   * Per-field match mode for `filters`; absence means exact ("exact" is
   * never serialized, keeping legacy URLs/views byte-identical). Wildcard:
   * `*`/`?` glob, case-insensitive. Regex: RE2, case-sensitive, `(?i)` opt-in.
   */
  filterModes?: Record<string, FieldMatchMode>;
  /** Per-field match mode for `exclusions` — one mode per key, applies to all its values. */
  exclusionModes?: Record<string, FieldMatchMode>;
  /** Annotation types to filter to ("tag" and/or "anomaly"), OR'd together */
  annotated?: ("tag" | "anomaly")[];
  /** Narrows the "tag" annotation type to a specific tag value */
  annotationTagValue?: string;
  /**
   * ID of a persisted detector run (from the Analysis tab's most recent
   * scan) — merged server-side with persisted anomaly annotations when
   * `annotated` includes "anomaly", so the filter also matches not-yet-
   * tagged findings. Derived from session state, not serialized to the
   * URL/saved views.
   */
  anomalyRunId?: string;
  /**
   * Event_id allowlist — e.g. results from a semantic search narrowing the
   * grid. Derived from session state, not serialized to the URL/saved views.
   */
  ids?: string[];
  limit?: number;
  offset?: number;
  /** Chronological sort direction (default: desc) */
  order?: "asc" | "desc";
}

/** Available field names for a timeline, returned by /fields */
export interface FieldsResponse {
  /** Fixed top-level columns present on every event */
  top_level: string[];
  /**
   * Dynamic keys aggregated from the attributes Map, including
   * enrichment-derived keys ("src_ip:geo_country") — all filterable.
   */
  attributes: string[];
  /**
   * Registered enrichers' output-field names (the `<field>` half of a
   * `<attr_key>:<field>` derived key) — lets the UI tell a real
   * enrichment-derived key apart from a raw vendor key that happens to
   * contain a colon, instead of guessing from the key name alone.
   */
  derived_suffixes: string[];
}

export interface HistogramBucket {
  start: string; // ISO datetime string
  count: number;
}

export interface HistogramResponse {
  interval_seconds: number;
  min: string | null;
  max: string | null;
  buckets: HistogramBucket[];
}

/** One chartable field from `viz/fields` — no anomaly heuristics applied. */
export interface VizFieldInfo {
  token: string;
  /** Number of distinct non-empty values. */
  distinct: number;
  /** Fraction of events with a non-empty value (0-1). */
  coverage: number;
}

/** All chartable fields for the Visualization page's field picker, sorted by coverage descending. */
export interface VizFieldsResponse {
  fields: VizFieldInfo[];
}

/** One value's count from a `viz/field-terms` terms aggregation. */
export interface FieldTermCount {
  value: string;
  count: number;
}

/** Top-N value/count terms aggregation for a field, honoring the active filters. */
export interface FieldTermsResponse {
  field: string;
  /** Total non-empty matching rows (across all values, not just the top-N returned). */
  total: number;
  /** Number of distinct non-empty values. */
  distinct: number;
  values: FieldTermCount[];
  /** Count of non-empty values outside the returned top-N — render as an "Other" slice. */
  other_count: number;
}

/** One fixed-width bin of a numeric field's value distribution. */
export interface FieldNumericBin {
  x0: number;
  x1: number;
  count: number;
}

/**
 * Summary statistics + fixed-width histogram for a numeric field.
 * `count === 0` means the field has no numeric values in the current filter
 * set — callers should fall back to treating it as categorical.
 */
export interface FieldNumericResponse {
  field: string;
  count: number;
  min: number | null;
  max: number | null;
  mean: number | null;
  stddev: number | null;
  /** Keyed by quantile, e.g. "0.5" (median), "0.25", "0.95", ... */
  quantiles: Record<string, number>;
  bins: FieldNumericBin[];
}

/** One time-bucketed series (a single field value's counts over time). */
export interface FieldTimeseriesSeries {
  value: string;
  buckets: HistogramBucket[];
}

/**
 * Per-value event counts bucketed over time, restricted to the top
 * `series_limit` values by overall count (see `vizApi.fieldTimeseries`).
 */
export interface FieldTimeseriesResponse {
  field: string;
  interval_seconds: number;
  min: string | null;
  max: string | null;
  series: FieldTimeseriesSeries[];
}

/** One shared-grid time bucket carrying both compare layers' raw counts. */
export interface CompareTimeBucket {
  start: string;
  primary: number;
  comparison: number;
}

/**
 * Two-layer event-count histogram from `viz/compare` (kind=time). Both
 * layers are evaluated against one shared bucket grid server-side, so the
 * series are comparable by construction.
 */
export interface CompareTimeResponse {
  kind: "time";
  interval_seconds: number;
  min: string | null;
  max: string | null;
  buckets: CompareTimeBucket[];
  primary_total: number;
  comparison_total: number;
}

/** One shared category carrying both compare layers' counts. */
export interface CompareTermValue {
  value: string;
  primary: number;
  comparison: number;
}

/** Two-layer terms aggregation from `viz/compare` (kind=terms) — the
 * primary's top-N fixes the category list for both layers. */
export interface CompareTermsResponse {
  kind: "terms";
  field: string;
  values: CompareTermValue[];
  distinct: number;
  primary_total: number;
  comparison_total: number;
  primary_other: number;
  comparison_other: number;
}

/** One shared-edge numeric bin carrying both compare layers' counts. */
export interface CompareNumericBin {
  x0: number;
  x1: number;
  primary: number;
  comparison: number;
}

/** Two-layer numeric histogram from `viz/compare` (kind=numeric) — bin
 * edges derive from the union min/max of both layers. */
export interface CompareNumericResponse {
  kind: "numeric";
  field: string;
  min: number | null;
  max: number | null;
  bins: CompareNumericBin[];
  primary_total: number;
  comparison_total: number;
}

/** A saved Visualization-page chart; `config` is a versioned ChartConfig
 * stored as opaque JSON (validated client-side by `parseStoredChartConfig`). */
export interface SavedChart {
  id: string;
  case_id: string;
  timeline_id: string;
  name: string;
  config: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

/** Body for export endpoint */
export interface ExportRequest {
  format: "csv" | "jsonl";
  filter: {
    q?: string;
    q_regex?: boolean;
    artifact?: string;
    artifacts?: string;
    source_id?: string;
    tag?: string;
    exclude_tag?: string;
    tags_include?: string;
    tags_exclude?: string;
    ids?: string;
    start?: string;
    end?: string;
    fields?: Record<string, string>;
    exclude?: Record<string, string[]>;
    field_modes?: Record<string, FieldMatchMode>;
    exclude_modes?: Record<string, FieldMatchMode>;
    annotated?: string;
    annotation_tag_value?: string;
    run_id?: string;
  };
}
