import { get, post } from "./client";
import type {
  AnomaliesResponse,
  Annotation,
  LogTemplatesResponse,
  NoveltyFieldsResponse,
  NumericFieldsResponse,
  TagAnomaliesResponse,
} from "./types";

export interface LogTemplatesParams {
  /** Field token to template over. Defaults to "message" (the indexed path). */
  field?: string;
  order?: "count" | "first_seen" | "last_seen";
  /** ID of a saved baseline definition. Required together with only_new. */
  baseline_id?: string;
  /** Keep only templates whose earliest occurrence is at/after the baseline's end. */
  only_new?: boolean;
  limit?: number;
  [key: string]: string | number | boolean | null | undefined;
}

export interface AnomalyParams {
  detector?: "value_novelty" | "value_combo" | "frequency" | "timestamp_order" | "numeric_range" | "charset" | "entropy" | "proportion_shift" | "interval_periodicity" | "sequence_novelty" | "sequence_motif" | "value_distribution_drift";
  /** Comma-separated field tokens for value_novelty, e.g. "artifact,display_name,attr:user_agent" */
  fields?: string;
  /** Field to group frequency series / build event sequences by */
  series_field?: string;
  /** |z| cutoff for the frequency detector. Omit to use the server default. */
  z_threshold?: number;
  /** Minimum backwards jump (seconds) for the timestamp_order detector. */
  min_skew_seconds?: number;
  /** BH false-discovery-rate ceiling (proportion_shift, interval_periodicity, value_distribution_drift). */
  fdr_q?: number;
  /** Effect-size floor (rate ratio) for the proportion_shift detector. */
  min_ratio?: number;
  /** Sequence length (n) for the sequence_novelty / sequence_motif detectors. Omit to use the server default. */
  ngram_size?: number;
  /** sequence_motif only: minimum occurrences before an n-gram counts as a motif. */
  min_support?: number;
  /** sequence_motif only: scope mining to events at/after this time (ISO, UTC). */
  start?: string;
  /** sequence_motif only: scope mining to events before this time (ISO, UTC). */
  end?: string;
  /** charset only: learn one reference alphabet per value of this field (e.g. per host). Omit for whole-scope learning. */
  group_field?: string;
  /** sequence_novelty / sequence_motif only: break an n-gram when consecutive events are more than this many seconds apart. Omit for no gap bound. */
  max_gap_seconds?: number;
  /** ID of a saved baseline definition (baseline range + suspect windows). Omit for self-baseline. */
  baseline_id?: string;
  limit?: number;
  /** Persist this scan as a DetectorRun and return its run_id (default: true). */
  persist?: boolean;
  /** Keep dismissed findings in `results`, flagged `dismissed: true` (default: false). */
  include_dismissed?: boolean;
  [key: string]: string | number | boolean | null | undefined;
}

export interface TagAnomalyParams extends AnomalyParams {
  // same shape as AnomalyParams — POST body
}

export const anomaliesApi = {
  list: (caseId: string, timelineId: string, params: AnomalyParams = {}) =>
    get<AnomaliesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies`,
      params,
    ),

  tag: (caseId: string, timelineId: string, params: TagAnomalyParams = {}) =>
    post<TagAnomaliesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies/tag`,
      params,
    ),

  /** Return candidate fields (with cardinality metadata) for the field picker. */
  fields: (caseId: string, timelineId: string) =>
    get<NoveltyFieldsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies/fields`,
    ),

  /** Return numeric-parseable candidate fields for the numeric-range detector. */
  numericFields: (caseId: string, timelineId: string) =>
    get<NumericFieldsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies/numeric-fields`,
    ),

  /** Browse structurally-distinct log-line shapes (W6). Not a scored detector. */
  logTemplates: (caseId: string, timelineId: string, params: LogTemplatesParams = {}) =>
    get<LogTemplatesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/log-templates`,
      params,
    ),

  /**
   * Persist a single live (not-yet-tagged) finding as a system annotation,
   * without re-running the detector or touching any other tagged finding —
   * the per-event "Persist" action in the event detail panel.
   */
  persistFinding: (
    caseId: string,
    sourceId: string,
    eventId: string,
    body: {
      detector: "value_novelty" | "value_combo" | "frequency" | "timestamp_order" | "numeric_range" | "charset" | "entropy" | "proportion_shift" | "interval_periodicity" | "sequence_novelty" | "sequence_motif" | "value_distribution_drift";
      content: string;
      details: Record<string, unknown>;
      /**
       * The comparison this verdict was reached under. `confirmed` is the only
       * disposition kind whose identity includes the scope, so omitting it here
       * collapses two claims (one per baseline) into one row — see
       * `disposition_identity` in db/postgres.py.
       */
      analysis_scope?: Record<string, unknown> | null;
    },
  ) =>
    post<{ annotation: Annotation }>(
      `/cases/${caseId}/sources/${sourceId}/events/${eventId}/anomalies/persist`,
      body,
    ),
};
