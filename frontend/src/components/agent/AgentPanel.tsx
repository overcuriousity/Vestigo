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
  type AgentFilterSpec,
  type AgentMessage,
  type AgentProposal,
  type AgentStreamEvent,
} from "@/api/agent";
import { useAgentStore } from "@/stores/agent";
import { triggerDownload } from "@/lib/download";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import { FindingCard } from "./FindingCard";
import { ProposalCard } from "./ProposalCard";
import { ToolSelectorPopover } from "./ToolSelector";
import { Markdown } from "./Markdown";
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
  | { kind: "user"; content: string }
  | {
      kind: "assistant";
      content: string;
      streaming?: boolean;
      promptTokens?: number | null;
      completionTokens?: number | null;
    }
  | { kind: "tool"; tool: string; args?: Record<string, unknown> | null }
  | { kind: "thinking"; content: string; streaming?: boolean }
  | { kind: "compaction"; summary: string }
  | {
      kind: "finding";
      title: string;
      description: string;
      spec: AgentFilterSpec;
      total?: number | null;
    }
  | { kind: "proposal"; proposalId: string }
  | { kind: "error"; detail: string };

function itemsFromMessages(messages: AgentMessage[]): ChatItem[] {
  const items: ChatItem[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      items.push({ kind: "user", content: m.content });
    } else if (m.role === "thinking") {
      if (m.content) items.push({ kind: "thinking", content: m.content });
    } else if (m.role === "compaction") {
      items.push({ kind: "compaction", summary: m.content });
    } else if (m.role === "assistant") {
      if (m.content) {
        items.push({
          kind: "assistant",
          content: m.content,
          promptTokens: m.prompt_tokens,
          completionTokens: m.completion_tokens,
        });
      }
    } else if (m.role === "tool" && m.tool_name === "propose_annotation") {
      // propose_annotation is rendered from its *result* row (which carries
      // proposal_id, the key into the proposals query) rather than the call
      // row (which only carries the proposed tag/comment/event_ids) — the
      // call row intentionally produces nothing.
      const result = m.tool_result as { proposal_id?: string } | null;
      if (result?.proposal_id) {
        items.push({ kind: "proposal", proposalId: result.proposal_id });
      }
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
          title: args.title ?? "Finding",
          description: args.description ?? "",
          spec: args.filters ?? {},
        });
      } else if (m.tool_name) {
        items.push({ kind: "tool", tool: m.tool_name, args: m.tool_args });
      }
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
}

const EMPTY_STREAM: StreamState = { items: [], liveText: "", liveThinking: "" };

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
  if (e.type === "compaction") {
    // A compaction mid-turn means the failed attempt is being retried — its
    // partial thinking will be re-streamed, so drop the stale deltas too.
    return {
      items: [...flushed, { kind: "compaction", summary: e.summary }],
      liveText: "",
      liveThinking: "",
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
            title: args.title ?? "Finding",
            description: args.description ?? "",
            spec: args.filters ?? {},
          },
        ],
        liveText: "",
      };
    }
    if (e.tool === "propose_annotation") {
      // Rendered from the paired tool_result below, once proposal_id is
      // known — the call event only carries the proposed tag/comment.
      return { ...s, items: flushed, liveText: "" };
    }
    return { ...s, items: [...flushed, { kind: "tool", tool: e.tool, args: e.args }], liveText: "" };
  }
  if (e.type === "tool_result") {
    // Most tool_result rows stay invisible (results feed the model, not
    // the analyst) — propose_annotation is the one exception, since its
    // proposal_id is only known once the result lands.
    if (e.tool === "propose_annotation") {
      const result = e.result as { proposal_id?: string } | null;
      return {
        ...s,
        items: result?.proposal_id
          ? [...flushed, { kind: "proposal", proposalId: result.proposal_id }]
          : flushed,
        liveText: "",
      };
    }
    return s;
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

function ToolRow({ tool, args }: { tool: string; args?: Record<string, unknown> | null }) {
  const summary = args && Object.keys(args).length > 0 ? JSON.stringify(args) : "";
  return (
    <div className="flex items-start gap-1.5 px-1 text-[11px] text-[var(--color-fg-secondary)]">
      <Wrench size={11} className="mt-0.5 shrink-0" />
      <span className="min-w-0 break-all font-mono">
        {tool}
        {summary && <span className="opacity-70"> {summary.slice(0, 200)}</span>}
      </span>
    </div>
  );
}

export function AgentPanel({ caseId, timelineId, currentFilters, onApplyFilters, onClose }: Props) {
  const storeKey = `${caseId}/${timelineId}`;
  const queryClient = useQueryClient();
  const panelWidth = useAgentStore((s) => s.panelWidth);
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
  const scrollRef = useRef<HTMLDivElement>(null);

  // Same query key the tool popover uses — dedupes onto one request and
  // doubles as the source for the always-visible OPSEC notice below.
  const infoQuery = useQuery({ queryKey: ["agent-info"], queryFn: agentApi.getInfo });
  const info = infoQuery.data;

  const conversationsQuery = useQuery({
    queryKey: ["agent-conversations", caseId, timelineId],
    queryFn: () => agentApi.listConversations(caseId, timelineId),
  });
  const conversations = conversationsQuery.data?.conversations ?? [];

  const conversationQuery = useQuery({
    queryKey: ["agent-conversation", caseId, activeId],
    queryFn: () => agentApi.getConversation(caseId, activeId!),
    enabled: !!activeId,
  });

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
    ? itemsFromMessages(conversationQuery.data.messages)
    : [];
  const liveItems = itemsFromStream(stream);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [persistedItems.length, liveItems.length, stream.liveText.length]);

  const sendTo = useCallback(
    async (conversationId: string) => {
      const content = input.trim();
      if (!content || streaming) return;
      setInput("");
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
            if (event.type === "tool_result" && event.tool === "propose_annotation") {
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
      void sendTo(conversation.id);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Could not start the conversation.");
    } finally {
      setCreating(false);
    }
  }, [caseId, timelineId, disabledTools, storeKey, queryClient, setActiveConversation, sendTo]);

  const send = useCallback(() => {
    if (!input.trim() || streaming || creating) return;
    if (!activeId) {
      void createAndSend();
      return;
    }
    void sendTo(activeId);
  }, [input, streaming, creating, activeId, createAndSend, sendTo]);

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

  const stop = useCallback(() => abortRef.current?.abort(), []);

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
      className="flex shrink-0 flex-col overflow-hidden border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]"
      style={{ width: panelWidth }}
      data-testid="agent-panel"
    >
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
            return <ToolRow key={i} tool={item.tool} args={item.args} />;
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
          if (item.kind === "finding") {
            return (
              <FindingCard
                key={i}
                title={item.title}
                description={item.description}
                spec={item.spec}
                total={item.total}
                onApply={onApplyFilters}
              />
            );
          }
          if (item.kind === "proposal") {
            const proposal = proposalsById[item.proposalId];
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
        {!activeId && (
          <div className="mb-1.5 flex items-center">
            <ToolSelectorPopover disabledTools={disabledTools} onChange={setDisabledTools} />
          </div>
        )}
        {createError && (
          <p className="mb-1.5 text-[11px] text-[var(--color-danger)]">
            Could not start the conversation: {createError}
          </p>
        )}
        <div className="flex items-end gap-1.5">
          <textarea
            className="max-h-32 min-h-[3.5rem] flex-1 resize-none rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]"
            placeholder="What should the agent look into?"
            value={input}
            disabled={streaming || creating}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          {streaming ? (
            <Tooltip content="Stop">
              <Button variant="outline" size="icon" onClick={stop}>
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
