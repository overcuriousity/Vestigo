/**
 * API client for the optional AI investigation agent.
 *
 * All endpoints 503 unless the backend reports `agent_available` in
 * /api/health — callers gate their UI on that flag, so these functions are
 * only reached when the agent is actually configured.
 *
 * The message endpoint streams SSE over a POST response; EventSource is
 * GET-only, so `streamMessage` reads the body via fetch + ReadableStream
 * (same wire format as useCaseStream's EventSource, parsed by hand).
 */
import { BASE, get, post, put, patch, del, fetchBlobGet, ApiError } from "./client";
import type { EventFilters, FieldMatchMode } from "./types";
import type { ChartConfig, ChartType } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { Metric } from "@/components/viz/lib/transforms";

/** Backend FilterSpec shape (snake_case) — what agent tool calls carry. */
export interface AgentFilterSpec {
  q?: string | null;
  q_regex?: boolean;
  artifacts?: string[] | null;
  source_id?: string | null;
  start?: string | null;
  end?: string | null;
  filters?: Record<string, string[]>;
  exclusions?: Record<string, string[]>;
  filter_modes?: Record<string, string>;
  exclusion_modes?: Record<string, string>;
  tags_include?: string[] | null;
  tags_exclude?: string[] | null;
  annotated?: ("tag" | "anomaly")[] | null;
  annotation_tag_value?: string | null;
  run_id?: string | null;
  event_ids?: string[] | null;
  collapse_routine?: boolean;
}

/**
 * Backend ChartSpec shape — carried verbatim on a `propose_chart` tool call's
 * `tool_args`. Mirrors `ChartConfig` field for field (snake_case), so anything
 * the analyst can build the agent can propose.
 */
export interface AgentChartSpecV2 {
  chart_type: ChartType;
  scale?: ChartConfig["scale"] | null;
  field?: string | null;
  field_y?: string | null;
  metric?: Metric | null;
  filters?: AgentFilterSpec | null;
  compare?: {
    mode: "off" | "baseline" | "custom";
    filters?: AgentFilterSpec | null;
  } | null;
  options?: {
    orientation?: "horizontal" | "vertical" | null;
    sort?: "count" | "value" | null;
    log_scale?: boolean | null;
    series_mode?: "overlay" | "stacked" | null;
    legend?: boolean | null;
    top_n?: number | null;
    bins?: number | null;
    buckets?: number | null;
    limit_x?: number | null;
    limit_y?: number | null;
    sample_limit?: number | null;
  } | null;
}

/**
 * The retired spec shape, whose single `kind` fused aggregation + mark +
 * compare-on. Still parsed because `propose_chart` calls are persisted as
 * message `tool_args` and old conversations re-render from them.
 */
export interface AgentChartSpecLegacy {
  kind:
    | "terms"
    | "numeric"
    | "timeseries"
    | "punchcard"
    | "pivot"
    | "scatter"
    | "compare_time"
    | "compare_terms"
    | "compare_numeric";
  field?: string | null;
  field_y?: string | null;
  filters?: AgentFilterSpec | null;
  comparison_filters?: AgentFilterSpec | null;
  buckets?: number | null;
  series_limit?: number | null;
  limit?: number | null;
  limit_y?: number | null;
}

export type AgentChartSpec = AgentChartSpecV2 | AgentChartSpecLegacy;

const isLegacySpec = (spec: AgentChartSpec): spec is AgentChartSpecLegacy =>
  !("chart_type" in spec);

/** Which `ChartType` (and matching `Scale`, per `CHART_META`) renders each
 * retired `propose_chart` kind. Frozen: these pairs are what historical chart
 * cards rendered as, so changing one rewrites the past. */
const CHART_TYPE_BY_KIND: Record<AgentChartSpecLegacy["kind"], ChartType> = {
  terms: "bar",
  numeric: "histogram",
  timeseries: "line",
  punchcard: "punchcard",
  pivot: "pivot",
  scatter: "scatter",
  compare_time: "time",
  compare_terms: "bar",
  compare_numeric: "histogram",
};
const SCALE_BY_KIND: Record<AgentChartSpecLegacy["kind"], ChartConfig["scale"]> = {
  terms: "nominal",
  numeric: "ratio",
  timeseries: "ratio",
  punchcard: "nominal",
  pivot: "nominal",
  scatter: "ratio",
  compare_time: "nominal",
  compare_terms: "nominal",
  compare_numeric: "ratio",
};

/**
 * Map a `propose_chart` spec onto the Visualize page's `ChartConfig` — the
 * shape every chart component, "Open in Visualize", and "Save" consume.
 * Mirrors `specToEventFilters` above: agent shapes translate to UI shapes at
 * the frontend boundary.
 */
export function specToChartConfig(spec: AgentChartSpec): ChartConfig {
  if (isLegacySpec(spec)) return specToChartConfigLegacy(spec);

  const o = spec.options ?? {};
  const options: ChartConfig["options"] = {};
  // `!= null` rather than a truthiness check: an explicit 0 is a value the
  // caller chose, and the old falsy guards silently dropped it.
  if (o.top_n != null) options.topN = o.top_n;
  if (o.bins != null) options.bins = o.bins;
  if (o.buckets != null) options.buckets = o.buckets;
  if (o.limit_x != null) options.limitX = o.limit_x;
  if (o.limit_y != null) options.limitY = o.limit_y;
  if (o.sample_limit != null) options.sampleLimit = o.sample_limit;
  if (o.orientation != null) options.orientation = o.orientation;
  if (o.sort != null) options.sort = o.sort;
  if (o.log_scale != null) options.logScale = o.log_scale;
  if (o.series_mode != null) options.seriesMode = o.series_mode;
  if (o.legend != null) options.legend = o.legend;

  const compare = spec.compare;
  return {
    v: 1,
    field: spec.field ?? null,
    fieldY: spec.field_y ?? null,
    // An omitted scale takes the chart type's default — the same value the
    // backend resolved and echoed in `resolved.scale`.
    scale: spec.scale ?? CHART_META[spec.chart_type].defaultScale,
    chartType: spec.chart_type,
    metric: spec.metric ?? "count",
    compare:
      compare?.mode === "baseline"
        ? { mode: "baseline" }
        : compare?.mode === "custom" && compare.filters
          ? { mode: "custom", filters: specToEventFilters(compare.filters) }
          : { mode: "off" },
    options,
  };
}

/**
 * Frozen translation of the retired `kind` shape. Its job is to reproduce
 * exactly what a historical chart card rendered — do not "improve" it, and do
 * not port fixes here from the current path (the overloaded `limit`, the
 * falsy guards, and `metric` always being "count" are all faithful).
 *
 * That includes `compare_*` without `comparison_filters` mapping to
 * `{mode: "off"}`: the old *backend* validated it as a baseline comparison,
 * but this function is what drew the card, and it drew one layer. The card is
 * the artifact, so the translation follows the card, not the validation.
 */
function specToChartConfigLegacy(spec: AgentChartSpecLegacy): ChartConfig {
  const isCompare = spec.kind.startsWith("compare_");
  const options: ChartConfig["options"] = {};
  if (spec.buckets) options.buckets = spec.buckets;
  if (spec.series_limit) options.topN = spec.series_limit;
  // `spec.limit` was overloaded — its meaning depended on `kind`, so each kind
  // routes it to the ChartConfig option its own data path actually read.
  if (spec.kind === "pivot") {
    if (spec.limit) options.limitX = spec.limit;
    if (spec.limit_y) options.limitY = spec.limit_y;
  } else if (spec.kind === "scatter") {
    if (spec.limit) options.sampleLimit = spec.limit;
  } else if (spec.kind === "numeric" || spec.kind === "compare_numeric") {
    if (spec.limit) options.bins = spec.limit;
  } else if (spec.limit) {
    options.topN = spec.limit;
  }
  return {
    v: 1,
    field: spec.field ?? null,
    fieldY: spec.field_y ?? null,
    scale: SCALE_BY_KIND[spec.kind],
    chartType: CHART_TYPE_BY_KIND[spec.kind],
    metric: "count",
    compare:
      isCompare && spec.comparison_filters
        ? { mode: "custom", filters: specToEventFilters(spec.comparison_filters) }
        : { mode: "off" },
    options,
  };
}

/** An agent-proposed annotation, propose→confirm (A1): the agent never
 * writes annotations directly — `propose_annotation` creates one of these,
 * and an analyst confirms or rejects it via the endpoints below. */
export interface AgentProposal {
  id: string;
  conversation_id: string;
  case_id: string;
  timeline_id: string;
  status: "proposed" | "confirmed" | "rejected";
  tag: string | null;
  comment: string | null;
  rationale: string;
  events: { source_id: string; event_id: string }[];
  created_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface AgentConversation {
  id: string;
  case_id: string;
  timeline_id: string;
  user_id: string;
  title: string;
  model_id: string | null;
  /** Per-chat tool restriction (null = none). Set at creation, adjustable
   * afterwards via `updateConversationTools` — a change applies from the next
   * turn on and is audited, never rewriting what earlier turns could do. */
  disabled_tools: string[] | null;
  /** Whether a turn is streaming for this conversation *right now* — live
   * process state, not a column. Lets a reopened panel show a working Stop
   * instead of an input that silently 409s. */
  active?: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentMessage {
  id: string;
  conversation_id: string;
  /** `compaction` and `fidelity` are marker rows: one degradation the runtime
   * applied mid-turn before re-running it. They also separate a retry's
   * re-executed tool rows from the attempt before it. */
  role: "user" | "assistant" | "tool" | "thinking" | "compaction" | "fidelity";
  content: string;
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
  tool_result: unknown;
  /** Provider-issued id shared by a tool call row and its result row — the
   * pairing key when a model batches parallel tool calls (results land in
   * completion order, not call order). Null on pre-migration rows. */
  tool_call_id?: string | null;
  created_at: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
}

/** One tool in the agent's catalog (GET /api/agent/info). */
export interface AgentToolInfo {
  name: string;
  description: string;
  embeddings_gated: boolean;
  requires_conversation: boolean;
  /** Hard-denied by the admin — cannot be re-enabled per user/chat. */
  admin_disabled: boolean;
  /** "core" tools form the lean preset for small-context models (A13).
   * Optional so an older backend still parses. */
  tier?: "core" | "extended";
}

/**
 * Non-admin agent config disclosure: powers the OPSEC notice ("evidence is
 * sent to {api_base_url}, processed by {model}") and the tool toggles in the
 * new-conversation dialog. Never contains the API key.
 */
export interface AgentInfo {
  model: string | null;
  provider: string;
  api_base_url: string | null;
  context_window: number | null;
  compact_threshold: number | null;
  tools: AgentToolInfo[];
  user_disabled_tools: string[];
}

/** The tiers `src/vestigo/agent/fidelity.py::Fidelity` can retry down to.
 * `auto` is a resolution mode, not a tier, so it never reaches the stream. */
export type AgentFidelity = "full" | "message" | "minimal";

export type AgentStreamEvent =
  | { type: "text_delta"; text: string }
  | { type: "thinking_delta"; text: string }
  | { type: "thinking"; text: string }
  | { type: "compaction"; summary: string; reason?: string }
  | { type: "fidelity"; fidelity: AgentFidelity; reason?: string }
  | { type: "tool_call"; tool_call_id: string; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; tool_call_id: string; tool: string; result: unknown }
  | {
      type: "done";
      content: string;
      prompt_tokens?: number | null;
      completion_tokens?: number | null;
    }
  /** The turn was stopped by an analyst (this client or another). The partial
   * turn is still persisted — a stopped turn stays part of the record. */
  | { type: "cancelled" }
  | { type: "error"; detail: string; code?: string };

/** Compact token count: 890, 12.4k, 1.2M. */
export function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

/** Map a backend FilterSpec onto the Explorer's EventFilters (camelCase). */
export function specToEventFilters(spec: AgentFilterSpec): EventFilters {
  const modes = (m?: Record<string, string>): Record<string, FieldMatchMode> | undefined => {
    if (!m) return undefined;
    const out: Record<string, FieldMatchMode> = {};
    for (const [k, v] of Object.entries(m)) {
      if (v === "wildcard" || v === "regex") out[k] = v;
    }
    return Object.keys(out).length > 0 ? out : undefined;
  };
  const f: EventFilters = {};
  if (spec.q) f.q = spec.q;
  if (spec.q_regex) f.qRegex = true;
  if (spec.artifacts?.length) f.artifacts = spec.artifacts;
  if (spec.source_id) f.sourceId = spec.source_id;
  if (spec.start) f.start = spec.start;
  if (spec.end) f.end = spec.end;
  if (spec.filters && Object.keys(spec.filters).length > 0) f.filters = spec.filters;
  if (spec.exclusions && Object.keys(spec.exclusions).length > 0) f.exclusions = spec.exclusions;
  const fm = modes(spec.filter_modes);
  if (fm) f.filterModes = fm;
  const em = modes(spec.exclusion_modes);
  if (em) f.exclusionModes = em;
  if (spec.tags_include?.length) f.tagsInclude = spec.tags_include;
  if (spec.tags_exclude?.length) f.tagsExclude = spec.tags_exclude;
  if (spec.annotated?.length) f.annotated = spec.annotated;
  if (spec.annotation_tag_value) f.annotationTagValue = spec.annotation_tag_value;
  if (spec.run_id) f.anomalyRunId = spec.run_id;
  if (spec.event_ids?.length) f.ids = spec.event_ids;
  if (spec.collapse_routine) f.collapseRoutine = true;
  return f;
}

export const agentApi = {
  /** Config + tool catalog for the current user (see AgentInfo). */
  getInfo: () => get<AgentInfo>(`/agent/info`),

  /** Persist the user's default tool selection for new conversations. */
  updatePreferences: (disabledTools: string[]) =>
    put<{ disabled_tools: string[] }>(`/agent/preferences`, { disabled_tools: disabledTools }),

  createConversation: (caseId: string, timelineId: string, disabledTools?: string[]) =>
    post<AgentConversation>(`/cases/${caseId}/agent/conversations`, {
      timeline_id: timelineId,
      ...(disabledTools && disabledTools.length > 0 ? { disabled_tools: disabledTools } : {}),
    }),

  listConversations: (caseId: string, timelineId?: string) =>
    get<{ conversations: AgentConversation[] }>(`/cases/${caseId}/agent/conversations`, {
      timeline_id: timelineId,
    }),

  getConversation: (caseId: string, conversationId: string) =>
    get<AgentConversation & { messages: AgentMessage[] }>(
      `/cases/${caseId}/agent/conversations/${conversationId}`,
    ),

  /** Adjust an existing conversation's tool set (audited server-side). */
  updateConversationTools: (caseId: string, conversationId: string, disabledTools: string[]) =>
    patch<AgentConversation>(`/cases/${caseId}/agent/conversations/${conversationId}`, {
      disabled_tools: disabledTools,
    }),

  /** Stop the turn streaming for this conversation. Idempotent — cancelling an
   * idle conversation reports `cancelled: false` rather than erroring. */
  cancelTurn: (caseId: string, conversationId: string) =>
    post<{ cancelled: boolean }>(
      `/cases/${caseId}/agent/conversations/${conversationId}/cancel`,
      {},
    ),

  deleteConversation: (caseId: string, conversationId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/agent/conversations/${conversationId}`),

  /** Full-thread JSON export (messages, tool calls, thinking, raw history). */
  exportConversation: (caseId: string, conversationId: string) =>
    fetchBlobGet(`/cases/${caseId}/agent/conversations/${conversationId}/export`),

  listProposals: (caseId: string, conversationId: string) =>
    get<{ proposals: AgentProposal[] }>(
      `/cases/${caseId}/agent/conversations/${conversationId}/proposals`,
    ),

  confirmProposal: (caseId: string, conversationId: string, proposalId: string) =>
    post<{ proposal: AgentProposal; written: number; skipped_event_ids: string[] }>(
      `/cases/${caseId}/agent/conversations/${conversationId}/proposals/${proposalId}/confirm`,
    ),

  rejectProposal: (caseId: string, conversationId: string, proposalId: string) =>
    post<{ proposal: AgentProposal }>(
      `/cases/${caseId}/agent/conversations/${conversationId}/proposals/${proposalId}/reject`,
    ),

  /**
   * Send a message and stream the agent's turn. Resolves once the stream
   * ends; `onEvent` fires for each SSE event as it arrives.
   */
  async streamMessage(
    caseId: string,
    conversationId: string,
    body: { content: string; view_filters?: EventFilters | null },
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(
      `${BASE}/cases/${caseId}/agent/conversations/${conversationId}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "include",
        signal,
      },
    );
    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        detail = ((await res.json()) as { detail?: string }).detail ?? detail;
      } catch {
        // ignore
      }
      throw new ApiError(res.status, detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line.
      for (;;) {
        const sep = buffer.indexOf("\n\n");
        if (sep === -1) break;
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data: ")) continue; // keepalives/comments
          try {
            onEvent(JSON.parse(line.slice(6)) as AgentStreamEvent);
          } catch {
            // Malformed frame — skip rather than kill the stream.
          }
        }
      }
    }
  },
};
