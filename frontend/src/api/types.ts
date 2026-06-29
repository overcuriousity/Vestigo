/** Typed API contract for TraceVector. Mirrors the FastAPI backend models. */

export interface Case {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Per-source field selection stored on a timeline after the embedding wizard.
 * Shape: { version: 1, sources: { "<source>": ["message", "attr:user_agent"] } }
 */
export interface EmbeddingFieldConfig {
  version: 1;
  sources: Record<string, string[]>;
}

export interface Timeline {
  id: string;
  case_id: string;
  name: string;
  description: string | null;
  parser: string | null;
  embedding_model: string | null;
  /** Analyst-defined per-source field selection, null when not yet configured. */
  embedding_config: EmbeddingFieldConfig | null;
  event_count: number;
  vector_count: number;
  created_at: string;
  updated_at: string;
}

export interface Event {
  event_id: string;
  case_id: string;
  timeline_id: string;
  source_file: string;
  byte_offset: number;
  line_number: number | null;
  content_hash: string;
  parser_name: string;
  parser_version: string;
  ingest_time: string;
  message: string;
  timestamp: string | null;
  timestamp_desc: string | null;
  source: string | null;
  source_long: string | null;
  display_name: string | null;
  /** Parser-derived tags (ClickHouse). Different from annotation tags. */
  tags: string[];
  attributes: Record<string, string>;
  embedding_model: string | null;
  embedding_config_hash: string | null;
  vector_id: string | null;
}

export interface EventPage {
  total: number;
  offset: number;
  limit: number;
  events: Event[];
}

export interface View {
  id: string;
  case_id: string;
  name: string;
  query: string;
  filter: Record<string, unknown>;
  created_at: string;
}

export type AnnotationType = "comment" | "tag" | "outlier" | "normal";
export type AnnotationOrigin = "user" | "system";

export interface Annotation {
  id: string;
  case_id: string;
  timeline_id: string;
  event_id: string;
  annotation_type: AnnotationType;
  content: string;
  origin: AnnotationOrigin;
  created_by: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed";
  progress: { total: number; processed: number } | null;
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

export interface OutlierDetails {
  /** "centroid-distance" | "normal-baseline" */
  method: string;
  distance: number;
  rank: number;
  of: number;
  /** Number of events sampled for global centroid. Present in centroid-distance mode. */
  sample_size?: number;
  /** Number of analyst-marked normal events. Present in normal-baseline mode. */
  baseline_size?: number;
  embedding_config_hash: string;
}

export interface AnomalyResult {
  event_id: string;
  score: number;
  event: Event;
  details: OutlierDetails;
}

export interface AnomaliesResponse {
  status: "ok" | "not_embedded" | "insufficient_vectors";
  /** "centroid-distance" | "normal-baseline" */
  method: string;
  sample_size: number;
  baseline_size: number;
  embedding_config_hash: string | null;
  results: AnomalyResult[];
}

export interface TagAnomaliesResponse extends AnomaliesResponse {
  tagged: number;
}

/** Per-source field info returned by /embedding-fields */
export interface EmbeddingSourceInfo {
  source: string;
  count: number;
  /** Fixed top-level fields available for embedding */
  top_level: string[];
  /** Dynamic attribute keys found for this source */
  attributes: string[];
  /** Recommended preselection (tokens like "message", "attr:user_agent") */
  recommended: string[];
}

export interface EmbeddingFieldsResponse {
  sources: EmbeddingSourceInfo[];
}

export interface UploadResult {
  timeline_id: string;
  events_parsed: number;
  events_inserted: number;
  parser: string;
  duplicate?: boolean;
}

export interface HealthResponse {
  status: "ok";
  version: string;
}

/** Filter params for the events query */
export interface EventFilters {
  q?: string;
  source?: string;
  tag?: string;
  excludeTag?: string;
  start?: string;
  end?: string;
  /** key=value field equality filters */
  filters?: Record<string, string>;
  /** key=value field exclusion filters */
  exclusions?: Record<string, string>;
  limit?: number;
  offset?: number;
  /** Chronological sort direction (default: desc) */
  order?: "asc" | "desc";
}

/** Available field names for a timeline, returned by /fields */
export interface FieldsResponse {
  /** Fixed top-level columns present on every event */
  top_level: string[];
  /** Dynamic keys aggregated from the attributes Map */
  attributes: string[];
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

/** Body for export endpoint */
export interface ExportRequest {
  format: "csv" | "jsonl";
  filter: {
    q?: string;
    source?: string;
    tag?: string;
    start?: string;
    end?: string;
    fields?: Record<string, string>;
    exclude?: Record<string, string>;
  };
}
