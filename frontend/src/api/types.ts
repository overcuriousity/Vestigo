/** Typed API contract for Vestigo. Mirrors the FastAPI backend models. */

export interface Case {
  id: string;
  name: string;
  description: string | null;
  /** Creator's user id. */
  owner_id: string | null;
  /** Investigation team this case belongs to, or null for a personal case. */
  team_id: string | null;
  /** A seeded demo case (fabricated data). Other users' copies are never listed. */
  is_demo: boolean;
  /** Caller's resolved access level, computed by the backend (api/deps.py). */
  access_level: "none" | "read" | "contribute" | "manage";
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
  /** False until the user finishes (or skips) the onboarding tour. */
  onboarding_completed: boolean;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
  /** Only present on /auth/me and /auth/me/password responses. */
  teams?: TeamMembershipSummary[];
  /**
   * Per-user UI state that has to outlive one browser — currently the agent's
   * `agent_disabled_tools` and `column_advisor_optin` (issue #213), a
   * `{ [timelineId]: true }` map of the timelines this user has opted in to AI
   * column suggestions on. Written through the whitelisted
   * `PUT /auth/me/preferences`.
   */
  preferences?: Record<string, unknown> | null;
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
  /**
   * Analyst-declared clock-skew correction in seconds (W2), applied at query
   * time only — never mutates events. 0 for the vast majority of sources.
   */
  time_offset_seconds: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  /** Generated converter that produced this Parquet source, when one did. */
  converter_script_id?: string | null;
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
  /** Data-derived default grid columns, shared by everyone with access. */
  recommended_columns: RecommendedColumns | null;
  /**
   * Analysis methods kept out of this timeline's unprompted sweep, shared by
   * everyone with access. A reading preference, never a gate — the analysis
   * plan ignores it and a muted method still runs when asked for by name.
   */
  muted_methods: string[];
  /**
   * Per-method field declarations: `{method_id: {field_token: boolean}}`, where
   * `true` pins a field into that detector's automatic selection and `false`
   * takes it out. Shared by everyone with access, and advice rather than a
   * gate — an explicit `fields` still scans an excluded field, and a run that
   * held one back says so in its warnings.
   */
  field_overrides: Record<string, Record<string, boolean>>;
  created_at: string;
  updated_at: string;
}

/**
 * A timeline's suggested event-grid columns (issue #213), derived from its own
 * field statistics rather than a fixed default.
 *
 * `status` is the whole contract: `running` while a job is in flight,
 * `ok` when `columns` should be applied, and `insufficient` when the backend
 * looked and found nothing worth suggesting — in which case the grid keeps
 * `DEFAULT_COLUMNS`. A per-user column choice always outranks this.
 */
export interface RecommendedColumns {
  status: "ok" | "insufficient" | "running";
  /** Grid column ids, `timestamp` first. Empty unless `status === "ok"`. */
  columns: string[];
  /** Column id -> why it was chosen; shown as a tooltip in the picker. */
  reasons: Record<string, string>;
  /** Which path produced this — the deterministic scorer, or the LLM on top. */
  method: "heuristic" | "llm";
  model: string | null;
  source_ids: string[];
  generated_at: string;
  job_id: string | null;
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
  /**
   * Attribute keys this row got from the timeline's `field_mappings` rather
   * than from its source file (db/field_mappings.py::project_mapped_fields).
   * Only present on the paged event query — absent on exports, on frozen
   * `event_ref` story blocks, and whenever the timeline defines no mappings.
   * A key stored under a canonical name is deliberately *not* listed: the
   * source file did carry it.
   */
  mapped_fields?: string[];
  embedding_model: string | null;
  embedding_config_hash: string | null;
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
  /**
   * Present only when the request set `collapse_routine`: distinct events
   * hidden by active routine-motif dispositions (0 when none) — the grid
   * must surface this so collapse is never silent.
   */
  routine_collapsed_count?: number;
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
  /** Set when this view was deleted while a story block still referenced it.
   *  Such views never appear in a list response; the field exists so a client
   *  resolving one directly can tell. */
  deleted_at?: string | null;
  created_at: string;
}

/**
 * The legacy "normal" type is gone: normality/dismissal/confirmation are
 * dispositions now (see `Disposition`); migration 0004 converted and
 * deleted the old rows.
 */
export type AnnotationType = "comment" | "tag" | "anomaly";
export type AnnotationOrigin = "user" | "system" | "agentic-analysis";

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
  /** Which detector produced this system annotation ("value_novelty" | "frequency"); null for human annotations. */
  detector: string | null;
}

/**
 * Analyst-visible annotations: written by a human, or by the agent and
 * confirmed by a human (origin "agentic-analysis"). Mirrors the backend's
 * USER_VISIBLE_ANNOTATION_ORIGINS — origin is provenance, not a visibility
 * class, so both render wherever user annotations do.
 */
export function isAnalystAnnotation(a: Pick<Annotation, "origin">): boolean {
  return a.origin === "user" || a.origin === "agentic-analysis";
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed";
  progress:
    | {
        /** Unit-count progress. Optional: a job may publish only a `phase`
         * before it knows how many items the phase covers. */
        total?: number;
        processed?: number;
        /** Coarse stage within a multi-stage job. Vocabulary is per `kind`:
         * `case_export` → queued|postgres|events|blobs|manifest
         * (transfer/exporter.py), `case_import` → queued|verify|postgres|
         * events|blobs|stats (transfer/importer.py). The same token means
         * opposite directions in the two, so always resolve copy through
         * `lib/jobPhases.ts` keyed on `kind`. */
        phase?: string;
        /** `convert_ingest`: generation round counters and the script the job
         * produced (also present on failure, so the tray can link to it). */
        attempt?: number;
        max_attempts?: number;
        converter_script_id?: string | null;
        /** Size of the received archive (`case_import` only, set at job
         * creation). Note `JobStore.update` *merges* progress dicts
         * (core/jobs.py), so this survives every later phase write. */
        bytes?: number;
        /** Total archive bytes (`case_export`, emitted on completion). */
        bytes_total?: number;
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
  case_id?: string | null;
  created_at?: number;
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
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
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
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One rare / first-seen field *combination* from the value_combo detector. */
export interface ValueComboFinding {
  type: "value_combo";
  /** The combined field tokens, in order. */
  fields: string[];
  /** The combination's values, aligned with `fields`. */
  values: string[];
  count: number;
  /** -log(count/total) — higher is rarer. */
  score: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One out-of-range numeric value from the numeric_range detector. */
export interface NumericRangeFinding {
  type: "numeric_range";
  field: string;
  value: number;
  count: number;
  /** excess distance beyond the band ÷ band width. */
  score: number;
  direction: "below" | "above";
  lower: number;
  upper: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One value containing never-seen characters from the charset detector. */
export interface CharsetFinding {
  type: "charset";
  field: string;
  value: string;
  /** Characters in the value outside the field's reference character set. */
  novel_chars: string[];
  count: number;
  /** Sum of per-novel-char surprise — higher = more/rarer novel characters. */
  score: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One entropy-outlier value from the entropy detector. */
export interface EntropyFinding {
  type: "entropy";
  field: string;
  value: string;
  /** Shannon character entropy of the value, in bits. */
  entropy: number;
  count: number;
  /** excess distance beyond the entropy band ÷ band width. */
  score: number;
  direction: "below" | "above";
  lower: number;
  upper: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One value-share shift between windows from the proportion_shift detector. */
export interface ProportionShiftFinding {
  type: "proportion_shift";
  field: string;
  value: string;
  /** Occurrences in the suspect window; 0 = vanished (baseline-only). */
  count: number;
  baseline_count: number;
  /** baseline_count ÷ baseline window's event total. */
  baseline_rate: number;
  /** count ÷ suspect window's event total (0.5-smoothed when count = 0). */
  window_rate: number;
  /** window_rate ÷ baseline_rate. */
  rate_ratio: number;
  direction: "up" | "down";
  g_statistic: number;
  p_value: number;
  /** Benjamini–Hochberg adjusted p-value across every test in the run. */
  q_value: number;
  /** = g_statistic — used for ranking. */
  score: number;
  /** First occurrence in the suspect window; null when vanished. */
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One arrival-cadence change between windows from the interval_periodicity detector. */
export interface IntervalPeriodicityFinding {
  type: "interval_periodicity";
  field: string;
  value: string;
  /** "missed"/"accelerated" = cadence break; "new_regularity" = beaconing. */
  direction: "missed" | "accelerated" | "new_regularity";
  /** Occurrences in the suspect window; 0 = fully silent (maximal "missed"). */
  count: number;
  baseline_count: number;
  /** Median inter-arrival delta (seconds) per window; null below 2 occurrences. */
  baseline_median_interval: number | null;
  window_median_interval: number | null;
  /** stddev ÷ mean of the inter-arrival deltas; null when undefined. */
  baseline_cv: number | null;
  window_cv: number | null;
  /** Poisson-rate G (cadence break) or Greenwood G (new regularity). */
  statistic: number;
  p_value: number;
  /** Benjamini–Hochberg adjusted p-value across every test in the run. */
  q_value: number;
  /** −log10(p_value) — used for ranking across the two test families. */
  score: number;
  /** First occurrence in the suspect window; null when fully silent. */
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One never-seen-in-baseline event-order n-gram from the sequence_novelty detector. */
export interface SequenceNoveltyFinding {
  type: "sequence_novelty";
  /** Field token the sequence was built over (e.g. "artifact"). */
  field: string;
  /** The n-gram's values, oldest → newest. */
  values: string[];
  /** " → ".join(values) — display form and the allowlist key. */
  value: string;
  /** Occurrences of the n-gram in the suspect window. */
  count: number;
  /** −log(count ÷ window_ngram_total) — used for ranking. */
  score: number;
  /** Timestamp of the first event of the earliest occurrence in the window. */
  first_seen: string | null;
  /** First event of that earliest occurrence. */
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One out-of-order timestamp finding from the timestamp_order detector. */
export interface TimestampOrderFinding {
  type: "timestamp_order";
  source_id: string;
  event_id: string;
  /** Violating event's timestamp (ISO, UTC). */
  timestamp: string;
  /** Previous record's timestamp in file/record order (ISO, UTC). */
  prev_timestamp: string;
  /** prev_timestamp − timestamp, in seconds (always > 0). */
  skew_seconds: number;
  byte_offset: number;
  line_number: number;
  /** = skew_seconds — used for ranking. */
  score: number;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One whole-field distribution change between windows from the value_distribution_drift detector. */
export interface DistributionDriftFinding {
  type: "value_distribution_drift";
  field: string;
  /** Suspect-window label — the finding is per field, so the window names it. */
  window_label: string;
  /** "ks" (numeric Kolmogorov–Smirnov) | "g-test-k" (categorical G-test). */
  test: "ks" | "g-test-k";
  /** KS D statistic or the 2×k G statistic. */
  statistic: number;
  /** The floor-gated effect size: KS D again, or the total-variation distance. */
  effect: number;
  /** "up"/"down" (median shift) | "spread" (equal medians, shape change) | "mixed" (categorical). */
  direction: "up" | "down" | "spread" | "mixed";
  /** Field-bearing events on each side of the test. */
  baseline_n: number;
  window_n: number;
  p_value: number;
  /** Benjamini–Hochberg adjusted p-value across every test in the run. */
  q_value: number;
  /** −log10(p_value) — ranks both test families on one scale. */
  score: number;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

/** One recurring event-order n-gram from the sequence_motif miner. */
export interface SequenceMotifFinding {
  type: "sequence_motif";
  /** Field token the sequence was built over (e.g. "artifact"). */
  field: string;
  /** The n-gram's values, oldest → newest. */
  values: string[];
  /** " → ".join(values) — display form and the allowlist key. */
  value: string;
  /** Total occurrences across all scanned sources. */
  support: number;
  sources_count: number;
  /** Median inter-occurrence gap (seconds) of the most regular source. */
  period_seconds: number | null;
  /** stddev ÷ mean of that source's gaps; null under 3 occurrences. */
  cv: number | null;
  /** max(0, 1 − cv) — 1 is a metronome, 0 is no regularity signal. */
  regularity_score: number;
  /** log10(support) × (1 + regularity_score) — used for ranking. */
  score: number;
  first_seen: string | null;
  last_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
  /**
   * Present (true) when the only confirmed verdict on this event was reached
   * under a *different* comparison. The claim stands, but not for this scope —
   * so the row is marked rather than badged, and Confirm stays live.
   */
  confirmed_other_scope?: boolean;
}

export type AnomalyFinding =
  | ValueNoveltyFinding
  | ValueComboFinding
  | FrequencyFinding
  | TimestampOrderFinding
  | NumericRangeFinding
  | CharsetFinding
  | EntropyFinding
  | ProportionShiftFinding
  | IntervalPeriodicityFinding
  | SequenceNoveltyFinding
  | SequenceMotifFinding
  | DistributionDriftFinding;

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
  /** Non-fatal caveats about the run (tiny/unscoreable suspect windows, …). */
  warnings?: string[];
  /** Serialized window snapshot for temporal runs driven by a baseline definition. */
  windows?: AnalysisWindowsPayload | null;
  /**
   * Findings that survived suppression before the `limit` cap — when it
   * exceeds `results.length` the server truncated and the view offers
   * "load more".
   */
  total_findings?: number;
  /**
   * Findings hidden by `dismissed` dispositions (presentation-only noise
   * triage). Always reported so nothing is silently dropped; pass
   * `include_dismissed` to keep them in `results` flagged `dismissed: true`.
   */
  dismissed_count?: number;
}

/** One structurally-distinct log-line shape (W6). */
export interface LogTemplateRow {
  /** Decimal string — a UInt64 hash, always stringified to avoid JS number precision loss. */
  template_id: string;
  /** Normalized shape, e.g. "Allow TCP <IP>:<NUM> -> <IP>:<NUM>". */
  template: string;
  count: number;
  distinct_sources: number;
  first_seen: string | null;
  last_seen: string | null;
  /** One representative raw value of the templated field. */
  example: string;
}

export interface LogTemplatesResponse {
  field: string;
  /** Distinct templates matching the scope, before `limit`. */
  total_templates: number;
  templates: LogTemplateRow[];
}

/** One suspect window in a baseline definition (half-open [start, end)). */
export interface SuspectWindow {
  id?: string;
  label: string;
  start: string;
  end: string;
}

/** The window snapshot echoed on a temporal AnomaliesResponse / stored in a run. */
export interface AnalysisWindowsPayload {
  baseline: { start: string; end: string };
  suspect_windows: SuspectWindow[];
}

/** A saved baseline definition (baseline range + suspect windows) for a timeline. */
export interface BaselineDefinition {
  id: string;
  case_id: string;
  timeline_id: string;
  name: string;
  baseline: { start: string; end: string };
  suspect_windows: SuspectWindow[];
  config_hash: string;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface BaselineListResponse {
  baselines: BaselineDefinition[];
}

export interface BaselineMutationResponse {
  baseline: BaselineDefinition;
  warnings: string[];
}

/** Analyst verdict on a finding — the unified disposition taxonomy. */
export type DispositionKind = "normal" | "dismissed" | "confirmed" | "routine";

/**
 * One analyst verdict on an anomaly finding. Scope is exactly one of value
 * (`field` + `value`, timeline-scoped) or event (`source_id` + `event_id`,
 * `timeline_id` null). `detector` is a detector id or `"*"` (all detectors).
 */
export interface Disposition {
  id: string;
  case_id: string;
  timeline_id: string | null;
  kind: DispositionKind;
  detector: string;
  field: string | null;
  value: string | null;
  source_id: string | null;
  event_id: string | null;
  note: string | null;
  details: Record<string, unknown> | null;
  /**
   * The comparison the verdict was reached under (`frame`, `baseline_id`).
   * Null on rows written before scope provenance existed. Only `confirmed`
   * folds it into the row's identity, so only that kind can hold two rows for
   * one finding — one per scope.
   */
  analysis_scope?: { frame?: string; baseline_id?: string | null } | null;
  created_by: string | null;
  created_at: string | null;
}

export interface DispositionListResponse {
  dispositions: Disposition[];
}

/** One UTC calendar day of disposition activity (triage burn-down source). */
export interface DispositionStatsDay {
  date: string; // "YYYY-MM-DD" (UTC)
  normal: number;
  dismissed: number;
  confirmed: number;
  routine: number;
  total: number;
  cumulative: {
    normal: number;
    dismissed: number;
    confirmed: number;
    routine: number;
    total: number;
  };
}

/**
 * Per-day disposition counts by kind (ascending, gaps not filled). Counts
 * reflect current rows only — deleted verdicts are not shown; the audit
 * trail records deletions.
 */
export interface DispositionStatsResponse {
  days: DispositionStatsDay[];
  totals: {
    normal: number;
    dismissed: number;
    confirmed: number;
    routine: number;
    total: number;
  };
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
  detector:
    | "value_novelty"
    | "value_combo"
    | "frequency"
    | "timestamp_order"
    | "numeric_range"
    | "charset"
    | "entropy"
    | "proportion_shift"
    | "interval_periodicity"
    | "sequence_novelty"
    | "value_distribution_drift";
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

/** One numeric-parseable field candidate from GET /anomalies/numeric-fields. */
export interface NumericFieldInfo {
  token: string;
  distinct: number;
  coverage: number;
  /** Fraction of non-empty values that parse as a number (0–1). */
  numeric_ratio: number;
  recommended: boolean;
}

export interface NumericFieldsResponse {
  fields: NumericFieldInfo[];
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

/** Optional subsystems that are hidden entirely when unconfigured. Keys match
 * `CAPABILITY_KEYS` in `src/vestigo/core/capabilities.py`. */
export interface Capabilities {
  embeddings: boolean;
  agent: boolean;
  mcp: boolean;
  oidc: boolean;
  enrichers: boolean;
  sigma: boolean;
  transfer: boolean;
  /** Demo-case seeding is enabled on this instance. */
  demo_case: boolean;
  /** The model may write converter scripts for plain-text uploads. */
  converter_generation: boolean;
  /** A saved converter script may be re-run over a new upload (switch on;
   * needs no model — the script sends nothing). */
  converter_reuse: boolean;
}

/**
 * `/api/health`'s `scan_budget` block: how the heavy-scan memory budget
 * resolved and against what. `risk` is the field an operator acts on — a
 * misconfiguration here has no symptom until ClickHouse is OOM-killed, and the
 * kernel does that without writing anything to ClickHouse's own log.
 */
export interface ScanBudget {
  risk: "ok" | "over_budget" | "unbounded";
  per_query_bytes: number;
  total_bytes: number;
  /** Cache maxima under the same ClickHouse ceiling, unavailable to scans. */
  cache_bytes: number;
  cache_breakdown: Record<string, number>;
  /** What is left under the ceiling for merges and allocator slack. */
  headroom_bytes: number | null;
  clickhouse_ceiling_bytes: number | null;
  clickhouse_ceiling_is_explicit: boolean;
  budget_ceiling_bytes: number | null;
  local_detected_bytes: number | null;
  source: "pinned" | "clickhouse" | "local";
  concurrency: number;
  pending_concurrency: number | null;
  max_threads: number;
  max_threads_source: "pinned" | "clickhouse_pinned" | "clickhouse" | "fallback";
  detected_cores: number | null;
  /** The chart lane (#300): its own gate, fed by the slot the heavy divisor reserves. */
  foreground: { concurrency: number; per_query_bytes: number };
}

/**
 * `/api/health`. Everything below `oidc_enabled` requires a session: which
 * optional subsystems an instance runs is an inventory of its attack surface,
 * while the login page legitimately needs to know the app is up and whether to
 * render the SSO button. Hence the optional fields — an anonymous response
 * carries only the first three.
 */
export interface HealthResponse {
  status: "ok";
  version: string;
  oidc_enabled: boolean;
  /** One entry per optional subsystem — false means "unconfigured", and the UI
   * renders no entry point for it. The flat flags below are older aliases. */
  capabilities?: Capabilities;
  /** False when neither local embedding deps nor a remote endpoint are configured. */
  embeddings_available?: boolean;
  /**
   * True only when VESTIGO_AGENT_* is configured and the LLM endpoint
   * answered the backend's probe — the agent UI renders nothing otherwise.
   */
  agent_available?: boolean;
  /** True when the MCP server endpoint (/mcp) is enabled and token issuance is available. */
  mcp_enabled?: boolean;
  /**
   * The tag every annotated event carries, derived at read time rather than
   * stored (`ANNOTATED_TAG` in `api/routers/events.py`). Served instead of
   * mirrored so the resolver and the grid cannot end up naming different
   * tags — a copy here would drift silently, since a renamed tag stops
   * matching without raising anything.
   */
  annotated_tag?: string;
  /** How the heavy-scan budget resolved. Authenticated responses only — it
   * describes the host's memory layout. */
  scan_budget?: ScanBudget;
}

/**
 * Non-default field-filter match modes; "exact" is implied by absence.
 * "empty" is a presence predicate rather than a comparison: it carries no
 * value, and its value list is a `[""]` placeholder on the wire.
 */
export type FieldMatchMode = "wildcard" | "regex" | "empty";

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
  /** key=[values] field filters — multiple values per field are OR'd (IN);
   * distinct fields are AND'ed with each other and every other restriction */
  filters?: Record<string, string[]>;
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
  /**
   * Collapse events belonging to routine-motif occurrences (dispositions of
   * kind "routine"). The response then always carries
   * `routine_collapsed_count` — collapse is explicit, never silent. Session
   * state, not serialized to the URL/saved views.
   */
  collapseRoutine?: boolean;
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
  /** Number of distinct non-empty values; null for an unbounded virtual field. */
  distinct: number | null;
  /** Fraction of events with a non-empty value (0-1); null for a virtual field,
   * whose values are derived rather than measured against the data. */
  coverage: number | null;
  /** Display name — present only for the virtual `time:` fields. */
  label?: string;
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
/**
 * What a derived aggregation resolved its derivation to — the labels every
 * bin / part can take, in order, and (for width/log bins) the edges the
 * server computed from the data's range, so the caption can print them.
 */
export interface DeriveEcho {
  kind: "bins" | "time_part";
  labels: string[];
  mode?: string;
  edges?: number[];
  negative_bin?: boolean;
  part?: string;
  timezone?: string;
}

export interface FieldTermsResponse {
  field: string;
  /** Present (non-null) when the request carried a `derive`. */
  derive?: DeriveEcho | null;
  /** Total non-empty matching rows (across all values, not just the top-N returned). */
  total: number;
  /** Number of distinct non-empty values. */
  distinct: number;
  values: FieldTermCount[];
  /** Count of non-empty values outside the returned top-N — render as an "Other" slice. */
  other_count: number;
  /**
   * True when the answer came from the per-source `field_stats` cache instead
   * of a live ClickHouse scan (unfiltered queries, `limit <= 50` only). Counts
   * are exact sums either way, but `distinct` is max-across-sources and the
   * top-N merge across sources is approximate — so the same field can rank its
   * values differently once a request falls through to the live path. Absent
   * on a live answer.
   */
  cached?: boolean;
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
  /** Population skewness g₁ — null for degenerate distributions (n < 2,
   * zero variance). Sign convention: > 0 right-skewed, < 0 left-skewed. */
  skewness: number | null;
  /** Keyed by quantile, e.g. "0.5" (median), "0.25", "0.95", ... */
  quantiles: Record<string, number>;
  bins: FieldNumericBin[];
  /** How the bin count was chosen. "fd": the Freedman–Diaconis rule produced
   * it. "fd_fallback": the rule was undefined (no interquartile spread) and a
   * fixed default was used instead — the caption must NOT credit FD for these.
   * "manual": an explicit bins request. */
  bin_rule: "fd" | "fd_fallback" | "manual";
  /** True when an "fd" count hit the allowed bin-count range and was clamped,
   * so the drawn width is not exactly the width the rule asked for. */
  bin_count_clamped: boolean;
  bin_width: number | null;
  /** Uniform sample of raw values for the strip overlay, drawn in a stable
   * hash order so an identical query redraws identical points — only
   * present when the request opted in (`points=true`). */
  points: { total: number; shown: number; values: number[] } | null;
}

/** One group's distribution inside a grouped box/violin response. */
export interface FieldNumericGroup {
  value: string;
  count: number;
  min: number | null;
  max: number | null;
  mean: number | null;
  stddev: number | null;
  skewness: number | null;
  quantiles: Record<string, number>;
  /** Binned over the response's GLOBAL [min, max] so groups compare. */
  bins: FieldNumericBin[];
}

/**
 * Per-group numeric distributions from `viz/field-numeric-grouped` — one
 * numeric field split by a categorical grouping field. Groups outside the
 * top-N are omitted (never rolled into an "Other" box); `omitted_groups` /
 * `omitted_count` carry the truth for the caption.
 */
export interface FieldNumericGroupedResponse {
  kind: "numeric_grouped";
  field: string;
  group_field: string;
  total: number;
  min: number | null;
  max: number | null;
  distinct_groups: number;
  omitted_groups: number;
  omitted_count: number;
  groups: FieldNumericGroup[];
  /** Uniform sample across the kept groups: [group, value] pairs. Stable
   * across reruns of an identical query. */
  points: { total: number; shown: number; values: [string, number][] } | null;
}

/** One field pair inside a correlation matrix. */
export interface FieldCorrelationPair {
  x: string;
  y: string;
  /** Events where BOTH fields are numeric — each pair has its own n. */
  n: number;
  pearson: number | null;
  p_pearson: number | null;
  spearman: number | null;
  p_spearman: number | null;
}

/**
 * Pairwise correlations across several numeric fields, from
 * `viz/field-correlation`. Pairwise-complete: a field with sparse numeric
 * coverage shrinks only the pairs it takes part in.
 */
export interface FieldCorrelationResponse {
  kind: "corr";
  fields: string[];
  total: number;
  numeric_counts?: Record<string, number>;
  pairs: FieldCorrelationPair[];
  dropped_fields: { field: string; reason: string }[];
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
  derive?: DeriveEcho | null;
  interval_seconds: number;
  min: string | null;
  max: string | null;
  series: FieldTimeseriesSeries[];
}

/** One (day-of-week × hour-of-day) cell; `dow` is ISO (1=Mon … 7=Sun), UTC. */
export interface PunchcardCell {
  dow: number;
  hour: number;
  count: number;
}

/**
 * Event counts by (day-of-week × hour-of-day), UTC, from `viz/time-punchcard`.
 * Sparse — cells with zero events are omitted; the chart zero-fills the 7×24 grid.
 */
export interface PunchcardResponse {
  kind: "punchcard";
  total: number;
  max_count: number;
  cells: PunchcardCell[];
}

/** One co-occurrence cell; `""` on an axis means "outside that axis's top-N" (Other). */
export interface FieldPivotCell {
  x: string;
  y: string;
  count: number;
}

/**
 * Top-X × top-Y co-occurrence matrix for two fields from `viz/field-pivot`.
 * `total` counts only events where both fields are non-empty.
 */
export interface FieldPivotResponse {
  kind: "pivot";
  field_x: string;
  field_y: string;
  x_values: string[];
  y_values: string[];
  /** Distinct values on this axis — a *measured* count the axis may have been
   * truncated against, or, when the matching `*_bounded` is true, the size of
   * a statically-known `time:` domain that was charted whole. Only the former
   * can mean "there is more than you are seeing". */
  x_distinct: number;
  y_distinct: number;
  x_bounded: boolean;
  y_bounded: boolean;
  /** Present when the request carried `derive_x`; the axis is then bounded. */
  derive_x?: DeriveEcho | null;
  cells: FieldPivotCell[];
  total: number;
}

export type TableSortColumnWire =
  | "value"
  | "count"
  | "share"
  | "first_seen"
  | "last_seen"
  | "distinct_second";

export interface FieldTableRow {
  value: string;
  count: number;
  /** count / total — the share of the filtered slice's non-empty values. */
  share: number;
  first_seen: string | null;
  last_seen: string | null;
  /** Distinct non-empty values of `second_field` on this row; null without one. */
  distinct_second: number | null;
}

/** `GET …/viz/field-table` — the table figure (`EventQueryService.field_table`). */
export interface FieldTableResponse {
  kind: "table";
  field: string;
  second_field: string | null;
  /** Events with a non-empty value — the share denominator. */
  total: number;
  /** Distinct non-empty values. */
  distinct: number;
  rows: FieldTableRow[];
  /** Present exactly when values were cut by the top-N. */
  remainder: { count: number; share: number; distinct_values: number } | null;
  sort: { by: TableSortColumnWire; dir: "asc" | "desc" };
  derive?: DeriveEcho | null;
}

/**
 * Uniform sample of numeric (x, y) pairs from `viz/field-scatter`, drawn in
 * a stable hash order so an identical query redraws identical points.
 * Extents describe the FULL data, not the sample; `total === 0` means one or
 * both fields have no numeric values — fall back to a categorical hint.
 */
/**
 * Server-computed correlation/regression block for a scatter pair. Pearson,
 * Spearman and the regression line are computed over the FULL pairwise-
 * complete data in ClickHouse; Kendall and Shapiro–Wilk over the drawn
 * sample (their `basis`/`n` say so). Nullable throughout — degenerate data
 * nulls a coefficient rather than failing the chart.
 */
export interface ScatterStats {
  n: number;
  basis: "full";
  pearson: { r: number | null; p: number | null };
  spearman: { rho: number | null; p: number | null };
  kendall: { tau: number | null; p: number | null; basis: "sample"; n: number } | null;
  regression: { slope: number | null; intercept: number | null; r_squared: number | null } | null;
  shapiro: {
    x: { w: number | null; p: number | null } | null;
    y: { w: number | null; p: number | null } | null;
    basis: "sample";
    n: number;
  };
  recommendation: "pearson" | "spearman";
  /** Where `recommendation` came from. "shapiro": both axes were tested and
   * the verdict follows. "default": normality could not be tested at all, so
   * Spearman is the conservative fallback and nothing measured it — the UI
   * must not present it as a verdict. */
  recommendation_basis: "shapiro" | "default";
}

export interface FieldScatterResponse {
  kind: "scatter";
  field_x: string;
  field_y: string;
  total: number;
  sampled: number;
  x_min: number | null;
  x_max: number | null;
  y_min: number | null;
  y_max: number | null;
  points: [number, number][];
  stats: ScatterStats | null;
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
  derive?: DeriveEcho | null;
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

/** The filter half of an export request — shared by both export surfaces, so a
 * value inventory always covers the same scope as an events export of the same view. */
export interface ExportFilterPayload {
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
  fields?: Record<string, string[]>;
  exclude?: Record<string, string[]>;
  field_modes?: Record<string, FieldMatchMode>;
  exclude_modes?: Record<string, FieldMatchMode>;
  annotated?: string;
  annotation_tag_value?: string;
  run_id?: string;
}

/** Body for export endpoint */
export interface ExportRequest {
  format: "csv" | "jsonl";
  filter: ExportFilterPayload;
}

/** Columns a value inventory can carry (#295). `value` is what the file is *of*,
 * so it is always emitted; the column the file is sorted by is appended when the
 * analyst has not ticked it. */
export type FieldInventoryColumn = "value" | "count" | "first_seen" | "last_seen";

/** Named separators rather than a free character — a delimited file is only useful
 * if whatever opens it can parse it. */
export type FieldInventorySeparator = "comma" | "semicolon" | "tab" | "pipe";

export type FieldInventoryOrder =
  | "count_desc"
  | "count_asc"
  | "value_asc"
  | "value_desc"
  | "first_seen_asc"
  | "first_seen_desc"
  | "last_seen_asc"
  | "last_seen_desc";

/** Body for the value-inventory export: one row per distinct value of `field`,
 * within the same filters an events export would use. */
export interface FieldInventoryRequest {
  field: string;
  columns: FieldInventoryColumn[];
  separator: FieldInventorySeparator;
  order_by: FieldInventoryOrder;
  filter: ExportFilterPayload;
}

// ---------------------------------------------------------------------------
// Stories (W7)
// ---------------------------------------------------------------------------

export type StoryBlockKind = "markdown" | "view_ref" | "chart_ref" | "event_ref";

export interface Story {
  id: string;
  case_id: string;
  title: string;
  description: string | null;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

/**
 * Per-kind block content, mirroring the backend's pydantic models
 * (`vestigo/stories/schemas.py`) field for field.
 *
 * Typed as a discriminated union rather than `Record<string, unknown>` on
 * purpose: the untyped version forced every renderer to re-assert the shape
 * locally, and a mismatch between what the server freezes and what a mark
 * reads then surfaces as a blank chart in a signed report instead of a build
 * failure. Narrow on `kind`, don't cast.
 */
export interface MarkdownBlockContent {
  text: string;
}

export interface ViewRefBlockContent {
  view_id: string;
  timeline_id: string;
  display?: { limit?: number; columns?: string[] | null };
}

export interface ChartRefBlockContent {
  chart_id: string;
  timeline_id: string;
}

export interface EventRefBlockContent {
  event_id: string;
  source_id: string;
  caption?: string | null;
}

interface StoryBlockBase {
  id: string;
  story_id: string;
  position: number;
  origin: "user" | "agent";
  version: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

export type StoryBlock =
  | (StoryBlockBase & { kind: "markdown"; content: MarkdownBlockContent })
  | (StoryBlockBase & { kind: "view_ref"; content: ViewRefBlockContent })
  | (StoryBlockBase & { kind: "chart_ref"; content: ChartRefBlockContent })
  | (StoryBlockBase & { kind: "event_ref"; content: EventRefBlockContent });

/** One variant of `StoryBlock`, selected by kind. */
export type StoryBlockOf<K extends StoryBlockKind> = Extract<StoryBlock, { kind: K }>;

export interface StoryExportMeta {
  id: string;
  story_id: string;
  case_id: string;
  snapshot_hash: string;
  html_hash: string | null;
  has_artifact: boolean;
  created_by: string;
  created_at: string;
}

export interface SnapshotResolution {
  executed_at: string;
  timeline_id?: string;
  error: string | null;
}

export interface SnapshotViewData {
  rows: Record<string, unknown>[];
  row_count_total: number;
  rows_included: number;
  truncated: boolean;
  columns: string[] | null;
}

export interface SnapshotChartData {
  name: string;
  config: Record<string, unknown>;
  /** The aggregation the server actually ran, which selects the mark. */
  resolved: { data_kind: string; compare_mode: string } | null;
  warnings: string[];
  /** Raw aggregation payload — reshape via `snapshotToChartResult`, never cast. */
  chart: unknown;
}

export interface SnapshotEventData {
  event: Record<string, unknown>;
  caption: string | null;
}

interface SnapshotBlockBase {
  id: string;
  origin: string;
  resolution: SnapshotResolution;
}

/**
 * One frozen block of an export snapshot, discriminated on `kind`.
 *
 * `data` is null exactly when the server could not resolve the block (see
 * `resolution.error`) — that pairing is the "honest gap" contract, so both
 * have to be checked together before rendering.
 */
export type SnapshotBlock =
  | (SnapshotBlockBase & {
      kind: "markdown";
      ref: MarkdownBlockContent;
      data: MarkdownBlockContent | null;
    })
  | (SnapshotBlockBase & {
      kind: "view_ref";
      ref: ViewRefBlockContent & { name?: string; query?: string; filter?: Record<string, unknown> };
      data: SnapshotViewData | null;
    })
  | (SnapshotBlockBase & {
      kind: "chart_ref";
      ref: ChartRefBlockContent & { name?: string };
      data: SnapshotChartData | null;
    })
  | (SnapshotBlockBase & {
      kind: "event_ref";
      ref: EventRefBlockContent;
      data: SnapshotEventData | null;
    });

export interface StorySnapshot {
  v: 1;
  story: {
    id: string;
    title: string;
    case_id: string;
    exported_at: string;
    exported_by: string;
  };
  blocks: SnapshotBlock[];
}
