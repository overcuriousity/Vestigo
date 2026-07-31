/**
 * AgentPanel (#203), live stream: a tool result folds into the call row it
 * answers, paired strictly by a non-empty tool_call_id. A provider that emits
 * an empty id must not splash one result across every unkeyed tool row.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAgentStore } from "@/stores/agent";
import type { AgentConversation, AgentStreamEvent } from "@/api/agent";

beforeAll(() => {
  Element.prototype.scrollTo = vi.fn();
});

const listConversationsMock = vi.fn();
const getConversationMock = vi.fn();
const listProposalsMock = vi.fn();
const streamMessageMock = vi.fn();
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
      streamMessage: (...args: unknown[]) => streamMessageMock(...args),
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

/**
 * Drive the panel's live turn with a canned event sequence and leave the stream
 * open: the panel clears its live transcript once a turn ends cleanly and
 * re-renders from the persisted rows, which is a different code path than the
 * one under test here.
 */
function streamsEvents(events: AgentStreamEvent[]) {
  streamMessageMock.mockImplementation(
    (
      _caseId: string,
      _convId: string,
      _body: unknown,
      onEvent: (e: AgentStreamEvent) => void,
    ) => {
      for (const event of events) onEvent(event);
      return new Promise<void>(() => {});
    },
  );
}

async function sendMessage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
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
  const box = await screen.findByPlaceholderText(/what should the agent look into/i);
  fireEvent.change(box, { target: { value: "check ssh" } });
  fireEvent.keyDown(box, { key: "Enter" });
}

beforeEach(() => {
  vi.clearAllMocks();
  useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
  listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
  listProposalsMock.mockResolvedValue({ proposals: [] });
  getConversationMock.mockResolvedValue({ ...conversation(), messages: [] });
});

describe("AgentPanel live tool results (#203)", () => {
  it("folds a streamed result into the call row with the same id", async () => {
    streamsEvents([
      { type: "tool_call", tool_call_id: "tc-1", tool: "query_events", args: { q: "ssh" } },
      { type: "tool_result", tool_call_id: "tc-1", tool: "query_events", result: { total: 7 } },
    ]);
    await sendMessage();
    const row = await screen.findByTestId("tool-call-tc-1");
    fireEvent.click(row.querySelector("summary")!);
    await waitFor(() => expect(row.textContent).toContain('"total": 7'));
  });

  it("does not fold a result whose tool_call_id is empty", async () => {
    // Two unkeyed calls: pairing is a guess, so the result is dropped rather
    // than shown on rows it may not belong to.
    streamsEvents([
      { type: "tool_call", tool_call_id: "", tool: "query_events", args: { q: "a" } },
      { type: "tool_call", tool_call_id: "", tool: "query_events", args: { q: "b" } },
      { type: "tool_result", tool_call_id: "", tool: "query_events", result: { total: 7 } },
    ]);
    await sendMessage();
    const rows = await screen.findAllByText(/query_events/);
    expect(rows.length).toBeGreaterThan(1);
    for (const row of rows) {
      const details = row.closest("details");
      if (!details) continue;
      fireEvent.click(details.querySelector("summary")!);
      expect(details.textContent).not.toContain('"total": 7');
    }
  });
});
