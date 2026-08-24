/**
 * AgentPanel (#203): a persisted tool CALL+RESULT row pair folds into one
 * expandable tool row — collapsed it shows the tool name and an args summary,
 * expanded it shows the exact arguments and what the tool returned.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAgentStore } from "@/stores/agent";
import type { AgentConversation, AgentMessage } from "@/api/agent";

beforeAll(() => {
  // jsdom has no scrollTo — AgentPanel auto-scrolls the transcript on update.
  Element.prototype.scrollTo = vi.fn();
});

const listConversationsMock = vi.fn();
const getConversationMock = vi.fn();
const listProposalsMock = vi.fn();
const getInfoMock = vi.fn().mockResolvedValue({
  api_base_url: "https://llm.example",
  model: "test-model",
  tools: [{ name: "query_events", description: "", admin_disabled: false }],
  user_disabled_tools: [],
});

vi.mock("@/api/agent", async () => {
  const actual = await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    agentApi: {
      listConversations: (...args: unknown[]) => listConversationsMock(...args),
      getConversation: (...args: unknown[]) => getConversationMock(...args),
      listProposals: (...args: unknown[]) => listProposalsMock(...args),
      getInfo: (...args: unknown[]) => getInfoMock(...args),
    },
  };
});

const CASE = "c1";
const TL = "t1";
const CONV_ID = "conv1";

function conversation(): AgentConversation {
  return {
    id: CONV_ID,
    case_id: CASE,
    timeline_id: TL,
    user_id: "u1",
    title: "Investigating",
    model_id: "m",
    disabled_tools: null,
    history_partial_at: null,
    created_at: null,
    updated_at: null,
  };
}

function toolPairMessages(): AgentMessage[] {
  return [
    {
      id: "m1",
      conversation_id: CONV_ID,
      role: "user",
      content: "check ssh",
      tool_name: null,
      tool_args: null,
      tool_result: null,
      created_at: null,
    } as AgentMessage,
    {
      id: "m2",
      conversation_id: CONV_ID,
      role: "tool",
      content: "",
      tool_name: "query_events",
      tool_args: { field: "user", value: "root" },
      tool_result: null,
      tool_call_id: "tc-1",
      created_at: null,
    } as AgentMessage,
    {
      id: "m3",
      conversation_id: CONV_ID,
      role: "tool",
      content: "",
      tool_name: "query_events",
      tool_args: null,
      tool_result: { total: 42, sample: ["evt-1"] },
      tool_call_id: "tc-1",
      created_at: null,
    } as AgentMessage,
    {
      id: "m4",
      conversation_id: CONV_ID,
      role: "assistant",
      content: "done",
      tool_name: null,
      tool_args: null,
      tool_result: null,
      created_at: null,
    } as AgentMessage,
  ];
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <AgentPanel
          caseId={CASE}
          timelineId={TL}
          currentFilters={{}}
          onApplyFilters={vi.fn()}
          onClose={vi.fn()}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
  listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
  listProposalsMock.mockResolvedValue({ proposals: [] });
  getConversationMock.mockResolvedValue({ ...conversation(), messages: toolPairMessages() });
});

describe("AgentPanel tool-call details (#203)", () => {
  it("pairs a persisted tool result with its call and shows it on expand", async () => {
    renderPanel();
    const row = await screen.findByTestId("tool-call-tc-1");
    // Collapsed by default: the detail payload stays folded away.
    expect(row.textContent).toContain("query_events");
    expect(row).not.toHaveAttribute("open");
    fireEvent.click(row.querySelector("summary")!);
    expect(row).toHaveAttribute("open");
    expect(row.textContent).toContain('"field": "user"');
    expect(row.textContent).toContain('"total": 42');
  });

  it("builds no payload bodies while the row is collapsed", async () => {
    // A <details> renders its children whether open or not, so the bodies are
    // mounted on demand instead — tool results are event lists, and formatting
    // every one on every panel render is a cost a folded row shouldn't pay.
    renderPanel();
    const row = await screen.findByTestId("tool-call-tc-1");
    expect(row.querySelectorAll("pre")).toHaveLength(0);
    fireEvent.click(row.querySelector("summary")!);
    expect(row.querySelectorAll("pre")).toHaveLength(2);
  });

  it("pairs on tool_call_id, so a call persisted with null args is still a call", async () => {
    // A zero-argument tool call whose provider persisted `null` rather than
    // `{}`. Discriminating call-vs-result on "has args" would read this row as
    // a result, let it consume the pending entry for tc-1, and fold tc-1's real
    // result onto the wrong call.
    const msgs = toolPairMessages();
    msgs.splice(2, 0, {
      id: "m1b",
      conversation_id: CONV_ID,
      role: "tool",
      content: "",
      tool_name: "list_sources",
      tool_args: null,
      tool_result: null,
      tool_call_id: "tc-0",
      created_at: null,
    } as AgentMessage);
    getConversationMock.mockResolvedValue({ ...conversation(), messages: msgs });

    renderPanel();
    // Rendered as its own call row rather than swallowed as a result.
    const noArgs = await screen.findByTestId("tool-call-tc-0");
    expect(noArgs.textContent).toContain("list_sources");
    // …and tc-1 still gets its own result, not nothing.
    const row = await screen.findByTestId("tool-call-tc-1");
    fireEvent.click(row.querySelector("summary")!);
    expect(row.textContent).toContain('"total": 42');
  });

  it("passes an orphaned result silently instead of rendering it as a call", async () => {
    // A result row whose call row is missing from the transcript. Identity
    // pairing finds nothing, so `tool_result` has to settle the half — and it
    // can, because the server writes args on a call row and a result on a
    // result row, never both. Without that, the row falls into the call branch
    // and renders as a bare, argument-less call the agent never made.
    const msgs = toolPairMessages();
    msgs.splice(1, 1); // drop tc-1's call row, keep its result
    getConversationMock.mockResolvedValue({ ...conversation(), messages: msgs });

    renderPanel();
    await screen.findByText("done");
    expect(screen.queryByTestId("tool-call-tc-1")).toBeNull();
    expect(screen.queryByText(/query_events/)).toBeNull();
  });

  it("keys the row by call id so open state cannot follow a position", async () => {
    // The row owns <details> open state. With an index key, a later row
    // appearing above it would hand that state to a different tool call.
    renderPanel();
    const row = await screen.findByTestId("tool-call-tc-1");
    fireEvent.click(row.querySelector("summary")!);
    expect(row).toHaveAttribute("open");

    const msgs = toolPairMessages();
    msgs.splice(1, 0, {
      id: "m0",
      conversation_id: CONV_ID,
      role: "thinking",
      content: "considering",
      tool_name: null,
      tool_args: null,
      tool_result: null,
      created_at: null,
    } as AgentMessage);
    getConversationMock.mockResolvedValue({ ...conversation(), messages: msgs });

    const again = await screen.findByTestId("tool-call-tc-1");
    expect(again.textContent).toContain("query_events");
  });
});
