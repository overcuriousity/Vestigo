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
import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Send, Sparkles, Square, Trash2, Wrench, X } from "lucide-react";

import {
  agentApi,
  type AgentFilterSpec,
  type AgentMessage,
  type AgentStreamEvent,
} from "@/api/agent";
import { useAgentStore } from "@/stores/agent";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import { FindingCard } from "./FindingCard";
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
  | { kind: "assistant"; content: string; streaming?: boolean }
  | { kind: "tool"; tool: string; args?: Record<string, unknown> | null }
  | {
      kind: "finding";
      title: string;
      description: string;
      spec: AgentFilterSpec;
      total?: number | null;
    }
  | { kind: "error"; detail: string };

function itemsFromMessages(messages: AgentMessage[]): ChatItem[] {
  const items: ChatItem[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      items.push({ kind: "user", content: m.content });
    } else if (m.role === "assistant") {
      if (m.content) items.push({ kind: "assistant", content: m.content });
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

function itemsFromStream(events: AgentStreamEvent[]): ChatItem[] {
  const items: ChatItem[] = [];
  let text = "";
  for (const e of events) {
    if (e.type === "text_delta") {
      text += e.text;
    } else if (e.type === "tool_call") {
      if (text) {
        items.push({ kind: "assistant", content: text });
        text = "";
      }
      if (e.tool === "propose_finding") {
        const args = e.args as {
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
      } else {
        items.push({ kind: "tool", tool: e.tool, args: e.args });
      }
    } else if (e.type === "error") {
      items.push({ kind: "error", detail: e.detail });
    }
    // "tool_result" rows stay invisible (results feed the model, not the
    // analyst); "done" is handled by the caller via query invalidation.
  }
  if (text) items.push({ kind: "assistant", content: text, streaming: true });
  return items;
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
  const [streamEvents, setStreamEvents] = useState<AgentStreamEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [pendingUserText, setPendingUserText] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

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

  const createMutation = useMutation({
    mutationFn: () => agentApi.createConversation(caseId, timelineId),
    onSuccess: (conversation) => {
      queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
      setActiveConversation(storeKey, conversation.id);
    },
  });

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
  const liveItems = itemsFromStream(streamEvents);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [persistedItems.length, liveItems.length, streamEvents.length]);

  const send = useCallback(async () => {
    const content = input.trim();
    if (!content || streaming) return;
    let conversationId = activeId;
    if (!conversationId) {
      const conversation = await agentApi.createConversation(caseId, timelineId);
      queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
      setActiveConversation(storeKey, conversation.id);
      conversationId = conversation.id;
    }
    setInput("");
    setPendingUserText(content);
    setStreamEvents([]);
    setStreaming(true);
    const abort = new AbortController();
    abortRef.current = abort;
    try {
      await agentApi.streamMessage(
        caseId,
        conversationId,
        { content, view_filters: currentFilters },
        (event) => setStreamEvents((prev) => [...prev, event]),
        abort.signal,
      );
    } catch (err) {
      if (!abort.signal.aborted) {
        const detail = err instanceof Error ? err.message : "Request failed";
        setStreamEvents((prev) => [...prev, { type: "error", detail }]);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      setPendingUserText(null);
      setStreamEvents([]);
      queryClient.invalidateQueries({ queryKey: ["agent-conversation", caseId, conversationId] });
      queryClient.invalidateQueries({ queryKey: ["agent-conversations", caseId, timelineId] });
    }
  }, [
    input,
    streaming,
    activeId,
    caseId,
    timelineId,
    storeKey,
    currentFilters,
    queryClient,
    setActiveConversation,
  ]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const items: ChatItem[] = [
    ...persistedItems,
    ...(pendingUserText ? [{ kind: "user", content: pendingUserText } as ChatItem] : []),
    ...liveItems,
  ];

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
            disabled={createMutation.isPending}
            onClick={() => setActiveConversation(storeKey, null)}
          >
            <Plus size={13} />
          </Button>
        </Tooltip>
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

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 space-y-2.5 overflow-y-auto p-2.5">
        {items.length === 0 && !streaming && (
          <p className="px-2 pt-6 text-center text-xs text-[var(--color-fg-secondary)]">
            Ask the agent to investigate this timeline — it searches, aggregates
            and runs detectors on its own, then proposes filters you can apply.
            It never changes your view by itself.
          </p>
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
                className="whitespace-pre-wrap px-1 text-xs leading-relaxed text-[var(--color-fg-primary)]"
              >
                {item.content}
                {item.streaming && <span className="animate-pulse">▌</span>}
              </div>
            );
          }
          if (item.kind === "tool") {
            return <ToolRow key={i} tool={item.tool} args={item.args} />;
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
        <div className="flex items-end gap-1.5">
          <textarea
            className="max-h-32 min-h-[3.5rem] flex-1 resize-none rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]"
            placeholder="What should the agent look into?"
            value={input}
            disabled={streaming}
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
              <Button variant="accent" size="icon" disabled={!input.trim()} onClick={send}>
                <Send size={13} />
              </Button>
            </Tooltip>
          )}
        </div>
      </div>
    </div>
  );
}
