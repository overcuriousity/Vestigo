/**
 * AgentPanel — chat surface for the optional AI investigation agent.
 *
 * Sandbox + apply model: the agent iterates against the backend in its own
 * loop and never mutates the analyst's view; findings arrive as cards whose
 * "Apply to Explorer" button writes the proposed filters into the URL via
 * the parent's setFilters. Rendered only when /api/health reports
 * `agent_available` (gated by the parent), so an unconfigured install shows
 * no trace of the feature.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  Brain,
  Download,
  Minimize2,
  Plus,
  Send,
  Sparkles,
  Square,
  Trash2,
  Wrench,
  X,
} from "lucide-react";

import {
  agentApi,
  formatTokenCount,
  parseToolArgObject,
  type AgentChartSpec,
  type AgentFilterSpec,
  type AgentMessage,
  type AgentProposal,
  type AgentStreamEvent,
} from "@/api/agent";
import { useAgentStore } from "@/stores/agent";
import { triggerDownload } from "@/lib/download";
import { Button } from "@/components/ui/Button";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import { FindingCard } from "./FindingCard";
import { ChartProposalCard } from "./ChartProposalCard";
import { ProposalCard } from "./ProposalCard";
import { StoryBlockProposalCard } from "./StoryBlockProposalCard";
import { ToolSelectorPopover } from "./ToolSelector";
import { AgentFiltersBar } from "./AgentFiltersBar";
import { FilterChips } from "@/components/explorer/FilterChips";
import { hasActiveFilters } from "@/lib/fieldFilters";
import {
  PROPOSAL_KIND_BY_ITEM,
  proposalItemKind,
  type ProposalItemKind,
} from "./proposalTools";
import { Markdown } from "./Markdown";
import { capPersistedForStream, type TurnBaseline } from "./transcript";
import type { EventFilters } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  /** The analyst's current Explorer filters — sent as context, never mutated. */
  currentFilters: EventFilters;
  onApplyFilters: (filters: EventFilters) => void;
  onClose: () => void;
}

/** One renderable chat item, unified over persisted rows and live stream events. */
type ChatItem =
  | {
      kind: "user";
      content: string;
      /** Persisted row id — keys the per-message filter stamp's testid. */
      messageId?: string;
      /** The Explorer filters the agent received with this message (#205).
       * Null/absent = no snapshot (pre-stamp rows, or none active). */
      filters?: EventFilters | null;
    }
  | {
      kind: "assistant";
      content: string;
      streaming?: boolean;
      promptTokens?: number | null;
      completionTokens?: number | null;
    }
  | {
      kind: "tool";
      /** The call's `tool_call_id` — pairs the row with its result and keys
       * the expandable detail (`data-testid`). Null on pre-migration rows. */
      id?: string | null;
      tool: string;
      args?: Record<string, unknown> | null;
      /** What the tool returned, paired from the result row (#203). */
      result?: unknown;
    }
  | { kind: "thinking"; content: string; streaming?: boolean }
  /** Historical marker rows from the retired compaction/fidelity mechanisms —
   * old transcripts still carry them, so they still render. */
  | { kind: "compaction"; summary: string }
  | { kind: "fidelity"; fidelity: string }
  /** The sliding context window acted: elided older results mid-turn ("fit")
   * or re-ran an overflowed turn under a derived budget ("overflow"). */
  | {
      kind: "window";
      reason: "fit" | "overflow";
      resultsElided: number;
      resultsTruncated: number;
      turnsDropped: number;
      duplicateCalls: number;
      resultsCapped: number;
    }
  /** `id` is the proposing call's `tool_call_id` where one exists. Card items
   * are the only ones wrapped in an ErrorBoundary, and a boundary keyed by
   * array index keeps its fallback when a later item shifts into that slot —
   * so these two carry an identity of their own. Null on pre-`tool_call_id`
   * rows, which fall back to the index like every other item. */
  | {
      kind: "finding";
      id?: string | null;
      title: string;
      description: string;
      spec: AgentFilterSpec;
      total?: number | null;
    }
  | { kind: "chart"; id?: string | null; title: string; description: string; spec: AgentChartSpec }
  | { kind: "proposal"; proposalId: string }
  | { kind: "storyProposal"; proposalId: string }
  | { kind: "error"; detail: string }
  /** Something happened that isn't a failure — a turn the analyst stopped. */
  | { kind: "notice"; detail: string };

interface ProposeChartArgs {
  title?: string;
  description?: string;
  spec?: AgentChartSpec;
}

interface PendingChart {
  id?: string | null;
  title: string;
  description: string;
  spec: AgentChartSpec;
}

/**
 * The proposal for a card, or null when it is missing from the query or is of
 * a kind that card cannot render.
 */
function proposalOfKind(
  proposal: AgentProposal | undefined,
  itemKind: ProposalItemKind,
): AgentProposal | null {
  if (!proposal) return null;
  return proposal.kind === PROPOSAL_KIND_BY_ITEM[itemKind] ? proposal : null;
}

function itemsFromMessages(messages: AgentMessage[]): ChatItem[] {
  const items: ChatItem[] = [];
  // propose_chart needs both its call row (title/description/spec) and its
  // paired result row (ok — no card on a failed validation) — buffered here
  // since the two arrive as separate rows, same pairing propose_annotation
  // resolves the other way (result-only, no buffering needed there).
  // Keyed by tool_call_id: models that batch parallel tool calls persist N
  // call rows followed by N result rows in *completion* order, so adjacency
  // pairing mislabels one card and drops the other N-1. Rows written before
  // the tool_call_id migration have no key to pair on, and are matched by
  // FIFO order *only while that is unambiguous* — see below.
  const pendingCharts = new Map<string, PendingChart>();
  const pendingChartFifo: PendingChart[] = [];
  // Generic tool rows (#203): the call row renders immediately and the result
  // row folds back into it. Same pairing discipline as the charts — keyed by
  // tool_call_id, FIFO only while a single unkeyed call is in flight.
  const openToolCalls = new Map<string, ChatItem & { kind: "tool" }>();
  const orphanToolCalls: (ChatItem & { kind: "tool" })[] = [];
  for (const m of messages) {
    const proposalKind = m.role === "tool" ? proposalItemKind(m.tool_name) : null;
    if (m.role === "user") {
      items.push({
        kind: "user",
        content: m.content,
        messageId: m.id,
        filters: (m.view_filters as EventFilters | null) ?? null,
      });
    } else if (m.role === "thinking") {
      if (m.content) items.push({ kind: "thinking", content: m.content });
    } else if (m.role === "compaction") {
      items.push({ kind: "compaction", summary: m.content });
    } else if (m.role === "fidelity") {
      // Historical rows: the tier the turn was re-run at (drop row's `to`).
      const drop = m.tool_result as { to?: string } | null;
      if (drop?.to) items.push({ kind: "fidelity", fidelity: drop.to });
    } else if (m.role === "window") {
      const stats = m.tool_result as {
        reason?: "fit" | "overflow";
        results_elided?: number;
        results_truncated?: number;
        turns_dropped?: number;
        duplicate_calls?: number;
        results_capped?: number;
      } | null;
      items.push({
        kind: "window",
        reason: stats?.reason === "overflow" ? "overflow" : "fit",
        resultsElided: stats?.results_elided ?? 0,
        // Absent on rows written before the truncation pass existed.
        resultsTruncated: stats?.results_truncated ?? 0,
        turnsDropped: stats?.turns_dropped ?? 0,
        // Absent on rows written before the request guard existed.
        duplicateCalls: stats?.duplicate_calls ?? 0,
        resultsCapped: stats?.results_capped ?? 0,
      });
    } else if (m.role === "assistant") {
      if (m.content) {
        items.push({
          kind: "assistant",
          content: m.content,
          promptTokens: m.prompt_tokens,
          completionTokens: m.completion_tokens,
        });
      }
    } else if (proposalKind) {
      // A proposal tool (see PROPOSAL_TOOLS) is rendered from its *result* row
      // — which carries proposal_id, the key into the proposals query — rather
      // than the call row, which only carries the proposed body (tag/comment,
      // block content). The call row intentionally produces nothing.
      const result = m.tool_result as { proposal_id?: string } | null;
      if (result?.proposal_id) {
        items.push({ kind: proposalKind, proposalId: result.proposal_id });
      }
    } else if (m.role === "tool" && m.tool_name === "propose_chart" && !m.tool_args) {
      // Result row: only a successful validation ("ok": true) gets a card —
      // a failed spec (bad kind, missing field) produced a tool error, no
      // proposal to show. A failed one still consumes its buffered call so
      // it cannot shift the pairing for its batch siblings.
      const result = m.tool_result as { ok?: boolean } | null;
      let chart: PendingChart | undefined;
      if (m.tool_call_id) {
        chart = pendingCharts.get(m.tool_call_id);
        pendingCharts.delete(m.tool_call_id);
      } else if (pendingChartFifo.length === 1) {
        // Unkeyed rows can only be paired by order, and order is not a fact
        // here: a failed call row is persisted before its validation error,
        // so with two or more buffered an `ok` result can pop the *rejected*
        // spec and draw a chart that does not match its own title. A card an
        // analyst reads as evidence must never be a guess — with the pairing
        // ambiguous, show nothing and let the transcript stand on its own.
        chart = pendingChartFifo.shift();
      } else {
        pendingChartFifo.length = 0;
      }
      if (result?.ok && chart) items.push({ kind: "chart", ...chart });
    } else if (m.role === "tool" && m.tool_args) {
      // Tool rows come in pairs (call with args, then result); render on the
      // call row and let the result row pass silently.
      if (m.tool_name === "propose_finding") {
        const args = m.tool_args as {
          title?: string;
          description?: string;
          filters?: AgentFilterSpec;
        };
        items.push({
          kind: "finding",
          id: m.tool_call_id,
          title: args.title ?? "Finding",
          description: args.description ?? "",
          spec: parseToolArgObject<AgentFilterSpec>(args.filters) ?? {},
        });
      } else if (m.tool_name === "propose_chart") {
        const args = m.tool_args as ProposeChartArgs;
        // A spec the provider stringified is still a usable spec; one that is
        // neither object nor parseable JSON renders nothing rather than
        // taking the whole conversation down.
        const spec = parseToolArgObject<AgentChartSpec>(args.spec);
        if (spec) {
          const chart: PendingChart = {
            id: m.tool_call_id,
            title: args.title ?? "Chart",
            description: args.description ?? "",
            spec,
          };
          if (m.tool_call_id) pendingCharts.set(m.tool_call_id, chart);
          else pendingChartFifo.push(chart);
        }
      } else if (m.tool_name) {
        // Unrelated call rows no longer clear the buffer: in a parallel batch
        // an intervening call between a chart's call row and its result row
        // is normal. An orphaned entry (result row never persisted) just
        // stays buffered and renders nothing.
        const item: ChatItem & { kind: "tool" } = {
          kind: "tool",
          id: m.tool_call_id,
          tool: m.tool_name,
          args: m.tool_args,
        };
        if (m.tool_call_id) openToolCalls.set(m.tool_call_id, item);
        else orphanToolCalls.push(item);
        items.push(item);
      }
    } else if (m.role === "tool" && !m.tool_args && m.tool_name) {
      // Generic result row (#203): fold the result into its call row instead
      // of passing silently. Unkeyed legacy rows pair FIFO only while that is
      // unambiguous — same rule as the chart pairing above.
      let target: (ChatItem & { kind: "tool" }) | undefined;
      if (m.tool_call_id) {
        target = openToolCalls.get(m.tool_call_id);
        openToolCalls.delete(m.tool_call_id);
      } else if (orphanToolCalls.length === 1) {
        target = orphanToolCalls.shift();
      } else {
        orphanToolCalls.length = 0;
      }
      if (target) target.result = m.tool_result;
    }
  }
  return items;
}

/**
 * Live-stream render state, folded incrementally: one `foldStreamEvent` call
 * per SSE event instead of re-deriving the whole item list from an
 * ever-growing event array on every delta (which was O(n²) over a turn).
 */
interface StreamState {
  items: ChatItem[];
  liveText: string;
  /** In-flight thinking segment; finalized by the terminal "thinking" event. */
  liveThinking: string;
  /** propose_chart call args keyed by tool_call_id, buffered until each
   * paired result's `ok` lands — see itemsFromMessages' matching comment.
   * A Map because parallel tool-call batches keep several in flight, with
   * results arriving in completion order. Never mutated in place — folds
   * copy on change, so EMPTY_STREAM's instance stays shared safely. */
  pendingCharts: ReadonlyMap<string, PendingChart>;
}

const EMPTY_STREAM: StreamState = {
  items: [],
  liveText: "",
  liveThinking: "",
  pendingCharts: new Map(),
};

function foldStreamEvent(s: StreamState, e: AgentStreamEvent): StreamState {
  if (e.type === "text_delta") {
    return { ...s, liveText: s.liveText + e.text };
  }
  if (e.type === "thinking_delta") {
    return { ...s, liveThinking: s.liveThinking + e.text };
  }
  if (e.type === "thinking") {
    // The terminal event carries the full segment — it replaces the
    // accumulated deltas rather than appending after them.
    return {
      ...s,
      items: [...s.items, { kind: "thinking", content: e.text }],
      liveThinking: "",
    };
  }
  const flushed: ChatItem[] = s.liveText
    ? [...s.items, { kind: "assistant", content: s.liveText }]
    : s.items;
  if (e.type === "window") {
    if (e.reason === "overflow") {
      // The overflowed attempt is being re-run — its partial text/thinking
      // will be re-streamed, so drop the stale deltas.
      return {
        ...s,
        items: [
          ...flushed,
          {
            kind: "window",
            reason: "overflow",
            resultsElided: 0,
            resultsTruncated: 0,
            turnsDropped: 0,
            duplicateCalls: 0,
            resultsCapped: 0,
          },
        ],
        liveText: "",
        liveThinking: "",
      };
    }
    // "fit": informational — the turn continued and this arrives just before
    // done, so nothing streamed gets dropped.
    return {
      ...s,
      items: [
        ...flushed,
        {
          kind: "window",
          reason: "fit",
          resultsElided: e.stats.results_elided,
          resultsTruncated: e.stats.results_truncated ?? 0,
          turnsDropped: e.stats.turns_dropped,
          duplicateCalls: e.stats.duplicate_calls ?? 0,
          resultsCapped: e.stats.results_capped ?? 0,
        },
      ],
      liveText: "",
    };
  }
  if (e.type === "tool_call") {
    if (e.tool === "propose_finding") {
      const args = e.args as {
        title?: string;
        description?: string;
        filters?: AgentFilterSpec;
      };
      return {
        ...s,
        items: [
          ...flushed,
          {
            kind: "finding",
            id: e.tool_call_id,
            title: args.title ?? "Finding",
            description: args.description ?? "",
            spec: parseToolArgObject<AgentFilterSpec>(args.filters) ?? {},
          },
        ],
        liveText: "",
      };
    }
    if (proposalItemKind(e.tool)) {
      // Rendered from the paired tool_result below, once proposal_id is
      // known — the call event only carries the proposed tag/comment (or the
      // block body). Falling through to the generic row here is what showed
      // a bare "propose_story_block" line instead of the card.
      return { ...s, items: flushed, liveText: "" };
    }
    if (e.tool === "propose_chart") {
      // Buffered until the paired tool_result reports ok — a failed spec
      // shows no card, same contract as itemsFromMessages. Keyed by
      // tool_call_id: parallel batches keep several proposals in flight.
      const args = e.args as { title?: string; description?: string; spec?: AgentChartSpec };
      const spec = parseToolArgObject<AgentChartSpec>(args.spec);
      let pendingCharts = s.pendingCharts;
      if (spec) {
        const next = new Map(s.pendingCharts);
        next.set(e.tool_call_id, {
          id: e.tool_call_id,
          title: args.title ?? "Chart",
          description: args.description ?? "",
          spec,
        });
        pendingCharts = next;
      }
      return { ...s, items: flushed, liveText: "", pendingCharts };
    }
    return {
      ...s,
      items: [...flushed, { kind: "tool", id: e.tool_call_id, tool: e.tool, args: e.args }],
      liveText: "",
    };
  }
  if (e.type === "tool_result") {
    // Most tool_result rows stay invisible (results feed the model, not the
    // analyst) — the proposal tools and propose_chart are exceptions, since
    // what they render is only known once the result lands.
    const proposalKind = proposalItemKind(e.tool);
    if (proposalKind) {
      const result = e.result as { proposal_id?: string } | null;
      return {
        ...s,
        items: result?.proposal_id
          ? [...flushed, { kind: proposalKind, proposalId: result.proposal_id }]
          : flushed,
        liveText: "",
      };
    }
    if (e.tool === "propose_chart") {
      // A failed validation still consumes its entry so it cannot shift the
      // pairing for its batch siblings; an orphaned result renders nothing.
      const result = e.result as { ok?: boolean } | null;
      const chart = s.pendingCharts.get(e.tool_call_id);
      let pendingCharts = s.pendingCharts;
      if (chart) {
        const next = new Map(s.pendingCharts);
        next.delete(e.tool_call_id);
        pendingCharts = next;
      }
      return {
        ...s,
        items: result?.ok && chart ? [...flushed, { kind: "chart", ...chart }] : flushed,
        liveText: "",
        pendingCharts,
      };
    }
    // Generic tool result (#203): fold it into the call row it answers so the
    // row can show what the tool returned, instead of dropping it outright.
    // The id must be non-empty to pair on: a provider that emitted "" for every
    // call would otherwise splash one result across every unkeyed row.
    if (!e.tool_call_id) return s;
    return {
      ...s,
      items: s.items.map((it) =>
        it.kind === "tool" && it.id === e.tool_call_id ? { ...it, result: e.result } : it,
      ),
    };
  }
  if (e.type === "cancelled") {
    // Flush whatever streamed before the stop — the partial turn is persisted
    // server-side, so dropping it here would make the live view disagree with
    // the record on reload.
    return { ...s, items: [...flushed, { kind: "notice", detail: "Turn stopped." }], liveText: "" };
  }
  if (e.type === "error") {
    return { ...s, items: [...flushed, { kind: "error", detail: e.detail }], liveText: "" };
  }
  // "done" is handled by the caller via query invalidation.
  return s;
}

function itemsFromStream(s: StreamState): ChatItem[] {
  const out = [...s.items];
  if (s.liveThinking) out.push({ kind: "thinking", content: s.liveThinking, streaming: true });
  if (s.liveText) out.push({ kind: "assistant", content: s.liveText, streaming: true });
  return out;
}

/** Cap on the rendered tool result — the full payload stays in the transcript
 * record; the row shows enough to audit the call without flooding the panel. */
function formatToolResult(result: unknown): string {
  const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
  return text.length > 4000 ? `${text.slice(0, 4000)}…` : text;
}

function ToolRow({
  id,
  tool,
  args,
  result,
}: {
  id?: string | null;
  tool: string;
  args?: Record<string, unknown> | null;
  result?: unknown;
}) {
  // A <details> renders its children whether or not it is open, so the bodies
  // are mounted on demand instead: tool results are event lists, and
  // stringifying every one of them on every panel render (there are dozens of
  // rows in a long transcript) is a cost the collapsed row shouldn't pay.
  const [open, setOpen] = useState(false);
  const hasArgs = !!args && Object.keys(args).length > 0;
  const summary = hasArgs ? JSON.stringify(args) : "";
  const argsText = useMemo(
    () => (open && hasArgs ? JSON.stringify(args, null, 2) : ""),
    [open, hasArgs, args],
  );
  const resultText = useMemo(
    () => (open && result !== undefined && result !== null ? formatToolResult(result) : ""),
    [open, result],
  );
  return (
    <details
      data-testid={id ? `tool-call-${id}` : undefined}
      // Left uncontrolled — the element owns its open state, `open` only
      // mirrors it to decide whether the bodies are worth building.
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      className="rounded border border-[var(--color-border)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
    >
      <summary
        onClick={() => setOpen((o) => !o)}
        className="flex cursor-pointer select-none items-center gap-1.5"
      >
        <Wrench size={11} className="shrink-0" />
        <span className="min-w-0 break-all font-mono">
          {tool}
          {summary && <span className="opacity-70"> {summary.slice(0, 200)}</span>}
        </span>
      </summary>
      {argsText && (
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words">
          {argsText}
        </pre>
      )}
      {resultText && (
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words">
          {resultText}
        </pre>
      )}
    </details>
  );
}

export function AgentPanel({ caseId, timelineId, currentFilters, onApplyFilters, onClose }: Props) {
  const storeKey = `${caseId}/${timelineId}`;
  const queryClient = useQueryClient();
  const panelWidth = useAgentStore((s) => s.panelWidth);
  const setPanelWidth = useAgentStore((s) => s.setPanelWidth);
  const activeId = useAgentStore((s) => s.activeConversationByTimeline[storeKey] ?? null);
  const setActiveConversation = useAgentStore((s) => s.setActiveConversation);

  const [input, setInput] = useState("");
  const [stream, setStream] = useState<StreamState>(EMPTY_STREAM);
  const [streaming, setStreaming] = useState(false);
  const [pendingUserText, setPendingUserText] = useState<string | null>(null);
  const [disabledTools, setDisabledTools] = useState<string[]>([]);
  const [createError, setCreateError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Message count at send time — see capPersistedForStream for why mid-stream
  // refetches must not render this turn's persisted rows a second time.
  const turnBaselineRef = useRef<TurnBaseline | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Same query key the tool popover uses — dedupes onto one request and
  // doubles as the source for the always-visible OPSEC notice below.
  const infoQuery = useQuery({ queryKey: ["agent-info"], queryFn: agentApi.getInfo });
  const info = infoQuery.data;

  // ── Resize drag (mirrors InvestigatePanel / EventDetailPanel) ──────────
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);
  const onDragStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragState.current = { startX: e.clientX, startWidth: panelWidth };
    },
    [panelWidth],
  );
  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragState.current) return;
      const delta = dragState.current.startX - e.clientX;
      setPanelWidth(Math.max(320, Math.min(900, dragState.current.startWidth + delta)));
    }
    function onMouseUp() {
      dragState.current = null;
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [setPanelWidth]);

  const conversationsQuery = useQuery({
    queryKey: ["agent-conversations", caseId, timelineId],
    queryFn: () => agentApi.listConversations(caseId, timelineId),
  });
  const conversations = conversationsQuery.data?.conversations ?? [];

  const conversationQuery = useQuery({
    queryKey: ["agent-conversation", caseId, activeId],
    queryFn: () => agentApi.getConversation(caseId, activeId!),
    enabled: !!activeId,
    // A turn started in another tab (or before this panel was reopened) is
    // only visible via `active`, and nothing pushes that to us — so poll
    // while one is running. Not while *this* panel streams: it already has
    // the turn's events first-hand. Once idle, the query goes quiet again.
    refetchInterval: (q) => (q.state.data?.active && !streaming ? 2000 : false),
  });

  // A turn is running that this panel is not itself streaming: the analyst
  // closed the panel or navigated away mid-turn and came back. Without this,
  // the input looks usable and every send 409s ("A turn is already running").
  const remoteTurnActive = !!conversationQuery.data?.active && !streaming;

  const proposalsQuery = useQuery({
    queryKey: ["agent-proposals", caseId, activeId],
    queryFn: () => agentApi.listProposals(caseId, activeId!),
    enabled: !!activeId,
  });
  const proposalsById = useMemo(() => {
    const map: Record<string, AgentProposal> = {};
    for (const p of proposalsQuery.data?.proposals ?? []) map[p.id] = p;
    return map;
  }, [proposalsQuery.data]);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => agentApi.deleteConversation(caseId, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
      setActiveConversation(storeKey, null);
    },
  });

  // Auto-scroll to the newest content while streaming or after a reload.
  const persistedItems = conversationQuery.data
    ? itemsFromMessages(
        capPersistedForStream(
          conversationQuery.data.messages,
          streaming,
          activeId,
          turnBaselineRef.current,
        ),
      )
    : [];
  const liveItems = itemsFromStream(stream);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [persistedItems.length, liveItems.length, stream.liveText.length]);

  const sendTo = useCallback(
    async (conversationId: string, persistedCount: number) => {
      const content = input.trim();
      if (!content || streaming) return;
      setInput("");
      turnBaselineRef.current = { conversationId, messageCount: persistedCount };
      setPendingUserText(content);
      setStream(EMPTY_STREAM);
      setStreaming(true);
      const abort = new AbortController();
      abortRef.current = abort;
      // Keep failed turns on screen: the finally block only clears the live
      // transcript when the turn ended cleanly, so an error item stays visible.
      let failed = false;
      try {
        await agentApi.streamMessage(
          caseId,
          conversationId,
          { content, view_filters: currentFilters },
          (event) => {
            if (event.type === "error") failed = true;
            setStream((prev) => foldStreamEvent(prev, event));
            if (event.type === "tool_result" && proposalItemKind(event.tool)) {
              // Every proposal kind is rendered from this query — without the
              // refetch the card resolves against a list fetched before the
              // proposal existed and degrades to a bare tool row.
              queryClient.invalidateQueries({
                queryKey: ["agent-proposals", caseId, conversationId],
              });
            }
          },
          abort.signal,
        );
      } catch (err) {
        if (!abort.signal.aborted) {
          failed = true;
          const detail = err instanceof Error ? err.message : "Request failed";
          setStream((prev) => foldStreamEvent(prev, { type: "error", detail }));
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
        // Await the transcript refetch before dropping the live items —
        // clearing first flashed the finished turn empty until data landed.
        await queryClient.invalidateQueries({
          queryKey: ["agent-conversation", caseId, conversationId],
        });
        queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
        setPendingUserText(null);
        if (failed) {
          // The persisted refetch already carries the user message and any
          // partial assistant text ("[interrupted]") — keep only the error
          // item(s) live so nothing renders twice.
          setStream((prev) => ({
            items: prev.items.filter((i) => i.kind === "error"),
            liveText: "",
            liveThinking: "",
            pendingCharts: new Map(),
          }));
        } else {
          setStream(EMPTY_STREAM);
        }
      }
    },
    [input, streaming, caseId, timelineId, currentFilters, queryClient],
  );

  // The OPSEC notice lives in the panel's empty state (always visible, no
  // "don't show again") — starting a conversation no longer needs a
  // blocking dialog on top of it. Tool selection is a toolbar popover.
  const [creating, setCreating] = useState(false);
  const createAndSend = useCallback(async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const conversation = await agentApi.createConversation(caseId, timelineId, disabledTools);
      queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
      setActiveConversation(storeKey, conversation.id);
      void sendTo(conversation.id, 0);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Could not start the conversation.");
    } finally {
      setCreating(false);
    }
  }, [caseId, timelineId, disabledTools, storeKey, queryClient, setActiveConversation, sendTo]);

  const send = useCallback(() => {
    // remoteTurnActive too: the server 409s a concurrent turn, so sending
    // here would only surface a confusing error.
    if (!input.trim() || streaming || creating || remoteTurnActive) return;
    if (!activeId) {
      void createAndSend();
      return;
    }
    void sendTo(activeId, conversationQuery.data?.messages.length ?? 0);
  }, [
    input,
    streaming,
    creating,
    remoteTurnActive,
    activeId,
    createAndSend,
    sendTo,
    conversationQuery.data,
  ]);

  const exportThread = useCallback(async () => {
    if (!activeId) return;
    setExporting(true);
    setExportError(null);
    try {
      const blob = await agentApi.exportConversation(caseId, activeId);
      // Titles are free user text — keep only filename-safe characters.
      const title = (conversationQuery.data?.title || activeId)
        .replace(/[^\p{L}\p{N}._-]+/gu, "_")
        .slice(0, 60);
      triggerDownload(blob, `agent-${title || activeId}.json`);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Export failed.");
    } finally {
      setExporting(false);
    }
  }, [activeId, caseId, conversationQuery.data]);

  // Aborting the local fetch only drops this client's SSE stream — with no
  // output flowing the server may not notice for a while, and the turn keeps
  // spending tokens. So tell the server too. Also the only thing that can
  // stop a turn this panel never streamed (remoteTurnActive).
  const stop = useCallback(() => {
    abortRef.current?.abort();
    if (activeId) {
      void agentApi.cancelTurn(caseId, activeId).finally(() => {
        queryClient.invalidateQueries({ queryKey: ["agent-conversation", caseId, activeId] });
      });
    }
  }, [caseId, activeId, queryClient]);

  // Tool set: before a conversation exists this seeds the next create; once
  // one is active it edits that conversation (audited server-side, effective
  // from the next turn). Seeded from the conversation so reopening the panel
  // shows what is actually in force, not a stale local guess.
  // `?? []`, not a truthiness check: an unrestricted conversation reports
  // `null`, and skipping those would leave the *previous* conversation's
  // restriction in local state — which the next toggle would then PATCH onto
  // this one, silently narrowing it and writing a misleading audit row.
  const conversationTools = conversationQuery.data?.disabled_tools;
  useEffect(() => {
    if (activeId) setDisabledTools(conversationTools ?? []);
    // No active conversation: back to a clean slate for the next create. The
    // popover remounts (see its `key`) and re-seeds from the user's defaults.
    else setDisabledTools([]);
  }, [activeId, conversationTools]);

  const toolsMutation = useMutation({
    mutationFn: (tools: string[]) => agentApi.updateConversationTools(caseId, activeId!, tools),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-conversation", caseId, activeId] });
    },
  });

  const handleToolsChange = useCallback(
    (tools: string[]) => {
      setDisabledTools(tools);
      if (activeId) toolsMutation.mutate(tools);
    },
    [activeId, toolsMutation],
  );

  const items: ChatItem[] = [
    ...persistedItems,
    ...(pendingUserText ? [{ kind: "user", content: pendingUserText } as ChatItem] : []),
    ...liveItems,
  ];

  // Conversation-wide token total, summed across persisted (loaded) messages.
  const loadedMessages = conversationQuery.data?.messages ?? [];
  let totalPromptTokens = 0;
  let totalCompletionTokens = 0;
  for (const m of loadedMessages) {
    if (m.prompt_tokens != null) totalPromptTokens += m.prompt_tokens;
    if (m.completion_tokens != null) totalCompletionTokens += m.completion_tokens;
  }
  const showTokenTotal = totalPromptTokens + totalCompletionTokens > 0;

  return (
    <div
      className="relative flex shrink-0 flex-col overflow-hidden border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]"
      style={{ width: panelWidth }}
      data-testid="agent-panel"
    >
      <div
        onMouseDown={onDragStart}
        className="absolute left-0 top-0 z-10 h-full w-1 cursor-col-resize opacity-0 transition-opacity hover:bg-[var(--color-accent)] hover:opacity-100"
        style={{ marginLeft: -2 }}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize agent panel"
      />

      {/* Header */}
      <div className="flex shrink-0 items-center gap-1.5 border-b border-[var(--color-border)] px-2.5 py-1.5">
        <Sparkles size={14} className="shrink-0 text-[var(--color-accent)]" />
        <span className="text-sm font-semibold">Agent</span>
        {showTokenTotal && (
          <span className="shrink-0 text-[10px] text-[var(--color-fg-secondary)]">
            Σ {formatTokenCount(totalPromptTokens)} in / {formatTokenCount(totalCompletionTokens)}{" "}
            out
          </span>
        )}
        <select
          className="ml-1 min-w-0 flex-1 truncate rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1.5 py-0.5 text-xs"
          value={activeId ?? ""}
          onChange={(e) => setActiveConversation(storeKey, e.target.value || null)}
        >
          <option value="">New conversation…</option>
          {conversations.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title || "Untitled"}
            </option>
          ))}
        </select>
        <Tooltip content="New conversation">
          <Button
            variant="ghost"
            size="icon"
            disabled={creating}
            onClick={() => setActiveConversation(storeKey, null)}
          >
            <Plus size={13} />
          </Button>
        </Tooltip>
        {activeId && (
          <Tooltip content="Export conversation as JSON">
            <Button variant="ghost" size="icon" disabled={exporting} onClick={exportThread}>
              <Download size={13} />
            </Button>
          </Tooltip>
        )}
        {activeId && (
          <Tooltip content="Delete conversation">
            <Button
              variant="ghost"
              size="icon"
              disabled={deleteMutation.isPending || streaming}
              onClick={() => deleteMutation.mutate(activeId)}
            >
              <Trash2 size={13} />
            </Button>
          </Tooltip>
        )}
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X size={14} />
        </Button>
      </div>

      {exportError && (
        <p className="border-b border-[var(--color-border)] px-2.5 py-1 text-[11px] text-[var(--color-danger)]">
          Export failed: {exportError}
        </p>
      )}

      {/* Inherited-filters transparency (#205): which Explorer view the next
          message sends as context. Read-only — editing stays in the Explorer. */}
      <AgentFiltersBar filters={currentFilters} />

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 space-y-2.5 overflow-y-auto p-2.5">
        {items.length === 0 && !streaming && (
          <div className="space-y-3 px-2 pt-6">
            <p className="text-center text-xs text-[var(--color-fg-secondary)]">
              Ask the agent to investigate this timeline — it searches, aggregates
              and runs detectors on its own, then proposes filters you can apply.
              It never changes your view by itself.
            </p>
            <div className="mx-auto flex max-w-xs items-start gap-1.5 rounded-md border border-[var(--color-warning)] bg-[var(--color-warning)]/10 p-2 text-left text-[11px] leading-relaxed">
              <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
              <p>
                <span className="font-semibold">Evidence leaves Vestigo.</span> Messages and tool
                results are sent to{" "}
                <span className="break-all font-mono font-semibold">
                  {info?.api_base_url ?? "the configured LLM endpoint"}
                </span>{" "}
                and processed by model{" "}
                <span className="font-mono font-semibold">{info?.model ?? "(unknown)"}</span>.
              </p>
            </div>
          </div>
        )}
        {items.map((item, i) => {
          if (item.kind === "user") {
            return (
              <div
                key={i}
                className="ml-6 whitespace-pre-wrap rounded-md bg-[var(--color-accent-dim)] px-2.5 py-1.5 text-xs text-[var(--color-fg-primary)]"
              >
                {item.content}
                {item.filters && hasActiveFilters(item.filters) && (
                  <div
                    data-testid={`message-filters-${item.messageId}`}
                    className="mt-1 border-t border-[var(--color-border)]/50 pt-1"
                  >
                    <span className="text-[10px] text-[var(--color-fg-secondary)]">
                      View at send:{" "}
                    </span>
                    <FilterChips filters={item.filters} />
                  </div>
                )}
              </div>
            );
          }
          if (item.kind === "assistant") {
            return (
              <div
                key={i}
                className="px-1 text-xs leading-relaxed text-[var(--color-fg-primary)]"
              >
                <Markdown content={item.content} />
                {item.streaming && <span className="animate-pulse">▌</span>}
                {item.promptTokens != null && item.completionTokens != null && (
                  <div className="mt-1 text-[10px] text-[var(--color-fg-secondary)]">
                    {formatTokenCount(item.promptTokens)} in /{" "}
                    {formatTokenCount(item.completionTokens)} out
                  </div>
                )}
              </div>
            );
          }
          if (item.kind === "tool") {
            return <ToolRow key={i} id={item.id} tool={item.tool} args={item.args} result={item.result} />;
          }
          if (item.kind === "thinking") {
            return (
              <details
                key={i}
                className="rounded border border-[var(--color-border)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
              >
                <summary className="flex cursor-pointer select-none items-center gap-1.5">
                  <Brain size={11} className="shrink-0" />
                  <span className={item.streaming ? "animate-pulse" : ""}>
                    {item.streaming ? "Thinking…" : "Thinking"}
                  </span>
                </summary>
                <div className="mt-1 whitespace-pre-wrap break-words">{item.content}</div>
              </details>
            );
          }
          if (item.kind === "compaction") {
            return (
              <details
                key={i}
                className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
              >
                <summary className="flex cursor-pointer select-none items-center gap-1.5">
                  <Archive size={11} className="shrink-0" />
                  <span>
                    Older turns were summarized to stay within the model's context window
                  </span>
                </summary>
                <div className="mt-1 whitespace-pre-wrap break-words">{item.summary}</div>
              </details>
            );
          }
          if (item.kind === "fidelity") {
            return (
              <div
                key={i}
                className="flex items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
              >
                {/* Not the compaction Archive: nothing was folded away here —
                    the same tools ran again, handing back less per record. */}
                <Minimize2 size={11} className="shrink-0" />
                <span>
                  Results did not fit the model's context window — retried with less
                  detail per event ({item.fidelity}).
                </span>
              </div>
            );
          }
          if (item.kind === "window") {
            return (
              <div
                key={i}
                className="flex items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
              >
                <Minimize2 size={11} className="shrink-0" />
                <span>
                  {item.reason === "overflow"
                    ? "The request exceeded the model's context window — retrying with older tool results elided."
                    : `Older tool results were elided to fit the model's context window (${item.resultsElided} elided${item.resultsTruncated ? `, ${item.resultsTruncated} truncated` : ""}${item.turnsDropped ? `, ${item.turnsDropped} turns dropped` : ""}${item.duplicateCalls ? `, ${item.duplicateCalls} duplicate calls collapsed` : ""}${item.resultsCapped ? `, ${item.resultsCapped} returns capped` : ""}). The full record is preserved in the transcript.`}
                </span>
              </div>
            );
          }
          if (item.kind === "finding") {
            return (
              <ErrorBoundary
                key={item.id ?? `i${i}`}
                resetKey={item.id ?? item.title}
                label="This finding"
              >
                <FindingCard
                  caseId={caseId}
                  timelineId={timelineId}
                  title={item.title}
                  description={item.description}
                  spec={item.spec}
                  total={item.total}
                  onApply={onApplyFilters}
                />
              </ErrorBoundary>
            );
          }
          if (item.kind === "chart") {
            return (
              // A chart card renders model-authored JSON that was persisted
              // verbatim and cannot be corrected after the fact, so a card
              // that cannot be drawn degrades to a notice rather than taking
              // the conversation — or the app — with it.
              <ErrorBoundary
                key={item.id ?? `i${i}`}
                resetKey={item.id ?? item.title}
                label="This chart proposal"
              >
                <ChartProposalCard
                  caseId={caseId}
                  timelineId={timelineId}
                  title={item.title}
                  description={item.description}
                  spec={item.spec}
                />
              </ErrorBoundary>
            );
          }
          if (item.kind === "proposal") {
            // The kind check is not redundant with the lookup: two item kinds
            // now resolve against the same query, and a card handed a
            // proposal of the other shape reads its payload off fields that
            // are null there. Fall back to the tool row instead.
            const proposal = proposalOfKind(proposalsById[item.proposalId], item.kind);
            if (!proposal || !activeId) {
              return <ToolRow key={i} tool="propose_annotation" />;
            }
            return (
              <ProposalCard
                key={i}
                caseId={caseId}
                conversationId={activeId}
                proposal={proposal}
                onApply={onApplyFilters}
              />
            );
          }
          if (item.kind === "storyProposal") {
            const proposal = proposalOfKind(proposalsById[item.proposalId], item.kind);
            if (!proposal || !activeId) {
              return <ToolRow key={i} tool="propose_story_block" />;
            }
            return (
              <StoryBlockProposalCard
                key={i}
                caseId={caseId}
                conversationId={activeId}
                proposal={proposal}
              />
            );
          }
          if (item.kind === "notice") {
            return (
              <p key={i} className="px-1 text-xs italic text-[var(--color-fg-muted)]">
                {item.detail}
              </p>
            );
          }
          return (
            <p key={i} className="px-1 text-xs text-[var(--color-danger)]">
              {item.detail}
            </p>
          );
        })}
        {streaming && liveItems.length === 0 && (
          <div className="flex items-center gap-2 px-1 text-xs text-[var(--color-fg-secondary)]">
            <Spinner size={12} /> Thinking…
          </div>
        )}
      </div>

      {/* Input */}
      <div className="shrink-0 border-t border-[var(--color-border)] p-2">
        <div className="mb-1.5 flex items-center gap-2">
          {/* Keyed so switching conversations (or starting a new one)
              remounts it: `seededRef` inside is mount-scoped, so without this
              a new chat would never re-seed from the user's saved defaults. */}
          <ToolSelectorPopover
            key={activeId ?? "new"}
            disabledTools={disabledTools}
            onChange={handleToolsChange}
            seedFromDefaults={!activeId}
          />
          {toolsMutation.isPending && <Spinner size={11} />}
          {activeId && !toolsMutation.isPending && disabledTools.length > 0 && (
            <span className="text-[10px] text-[var(--color-fg-muted)]">
              applies from the next turn
            </span>
          )}
        </div>
        {toolsMutation.isError && (
          <p className="mb-1.5 text-[11px] text-[var(--color-danger)]">
            Could not change the tool set:{" "}
            {toolsMutation.error instanceof Error ? toolsMutation.error.message : "unknown error"}
          </p>
        )}
        {remoteTurnActive && (
          <p className="mb-1.5 flex items-center gap-1.5 text-[11px] text-[var(--color-fg-secondary)]">
            <Spinner size={11} /> A turn is still running — stop it or wait for it to finish.
          </p>
        )}
        {createError && (
          <p className="mb-1.5 text-[11px] text-[var(--color-danger)]">
            Could not start the conversation: {createError}
          </p>
        )}
        <div className="flex items-end gap-1.5">
          <textarea
            className="max-h-32 min-h-[3.5rem] flex-1 resize-none rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]"
            placeholder={
              remoteTurnActive ? "Waiting for the running turn…" : "What should the agent look into?"
            }
            value={input}
            disabled={streaming || creating || remoteTurnActive}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          {streaming || remoteTurnActive ? (
            <Tooltip content={remoteTurnActive ? "Stop the running turn" : "Stop"}>
              <Button
                variant="outline"
                size="icon"
                onClick={stop}
                aria-label={remoteTurnActive ? "Stop the running turn" : "Stop"}
              >
                <Square size={13} />
              </Button>
            </Tooltip>
          ) : (
            <Tooltip content="Send (Enter)">
              <Button
                variant="accent"
                size="icon"
                disabled={!input.trim() || creating}
                onClick={send}
              >
                {creating ? <Spinner size={13} /> : <Send size={13} />}
              </Button>
            </Tooltip>
          )}
        </div>
      </div>
    </div>
  );
}
