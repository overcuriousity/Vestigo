/**
 * AgentPanel (A9): a persisted propose_chart CALL+RESULT row pair with
 * result.ok === true folds into exactly one ChartProposalCard — and a
 * failed validation (ok !== true, or a missing result) produces none. Mocks
 * ChartProposalCard itself to isolate itemsFromMessages' pairing logic from
 * the card's own data fetching (covered by chartProposalCard.test.tsx).
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
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
// The panel's OPSEC notice and the tool popover share one `agent-info` query;
// stubbed here so it resolves instead of failing into react-query's error path.
const getInfoMock = vi.fn().mockResolvedValue({
  api_base_url: "https://llm.example",
  model: "test-model",
  tools: [{ name: "search_events", description: "", admin_disabled: false }],
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

vi.mock("@/components/agent/ChartProposalCard", () => ({
  ChartProposalCard: (props: { title: string }) => (
    <div data-testid="chart-proposal-card">{props.title}</div>
  ),
}));

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
    created_at: null,
    updated_at: null,
  };
}

function chartMessages(resultOk: boolean | "missing"): AgentMessage[] {
  const call: AgentMessage = {
    id: "m1",
    conversation_id: CONV_ID,
    role: "tool",
    content: "",
    tool_name: "propose_chart",
    tool_args: {
      title: "Artifact spread",
      description: "top artifacts",
      spec: { kind: "terms", field: "artifact" },
    },
    tool_result: null,
    created_at: null,
  };
  if (resultOk === "missing") return [call];
  const result: AgentMessage = {
    id: "m2",
    conversation_id: CONV_ID,
    role: "tool",
    content: "",
    tool_name: "propose_chart",
    tool_args: null,
    tool_result: { ok: resultOk, total: 100 },
    created_at: null,
  };
  return [call, result];
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
});

/** One call/result row pair sharing a tool_call_id, as persisted rows. */
function batchRows(
  charts: { id: string | null; title: string; ok: boolean }[],
  order: "adjacent" | "batched" = "batched",
  resultIds?: string[],
): AgentMessage[] {
  const calls: AgentMessage[] = charts.map((c, i) => ({
    id: `call-${i}`,
    conversation_id: CONV_ID,
    role: "tool",
    content: "",
    tool_name: "propose_chart",
    tool_args: {
      title: c.title,
      description: "",
      spec: { kind: "terms", field: "artifact" },
    },
    tool_result: null,
    tool_call_id: c.id,
    created_at: null,
  }));
  const byId = new Map(charts.map((c) => [c.id, c]));
  const results: AgentMessage[] = (resultIds ?? charts.map((c) => c.id)).map((id, i) => ({
    id: `result-${i}`,
    conversation_id: CONV_ID,
    role: "tool",
    content: "",
    tool_name: "propose_chart",
    tool_args: null,
    tool_result: { ok: byId.get(id)?.ok ?? false, total: 100 },
    tool_call_id: id,
    created_at: null,
  }));
  if (order === "adjacent") return calls.flatMap((call, i) => [call, results[i]]);
  return [...calls, ...results];
}

describe("AgentPanel propose_chart folding", () => {
  it("a call+result pair with ok:true renders exactly one ChartProposalCard", async () => {
    getConversationMock.mockResolvedValue({ ...conversation(), messages: chartMessages(true) });
    renderPanel();
    const cards = await screen.findAllByTestId("chart-proposal-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent("Artifact spread");
  });

  it("a result with ok:false renders no card", async () => {
    getConversationMock.mockResolvedValue({ ...conversation(), messages: chartMessages(false) });
    renderPanel();
    // Let the conversation query settle, then assert nothing appeared.
    await screen.findByTestId("agent-panel");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("chart-proposal-card")).toBeNull();
  });

  it("a call row with no paired result renders no card", async () => {
    getConversationMock.mockResolvedValue({ ...conversation(), messages: chartMessages("missing") });
    renderPanel();
    await screen.findByTestId("agent-panel");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("chart-proposal-card")).toBeNull();
  });

  it("a parallel batch (N calls then N results) renders all N cards, correctly titled", async () => {
    const charts = [
      { id: "tc1", title: "Chart one", ok: true },
      { id: "tc2", title: "Chart two", ok: true },
      { id: "tc3", title: "Chart three", ok: true },
    ];
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: batchRows(charts),
    });
    renderPanel();
    const cards = await screen.findAllByTestId("chart-proposal-card");
    expect(cards.map((c) => c.textContent)).toEqual(["Chart one", "Chart two", "Chart three"]);
  });

  it("pairs by tool_call_id when results land in completion order, not call order", async () => {
    const charts = [
      { id: "tc1", title: "Slow chart", ok: true },
      { id: "tc2", title: "Fast chart", ok: true },
    ];
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: batchRows(charts, "batched", ["tc2", "tc1"]),
    });
    renderPanel();
    const cards = await screen.findAllByTestId("chart-proposal-card");
    expect(cards.map((c) => c.textContent)).toEqual(["Fast chart", "Slow chart"]);
  });

  it("a failed validation inside a batch consumes its entry without shifting siblings", async () => {
    const charts = [
      { id: "tc1", title: "Bad chart", ok: false },
      { id: "tc2", title: "Good chart", ok: true },
    ];
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: batchRows(charts),
    });
    renderPanel();
    const cards = await screen.findAllByTestId("chart-proposal-card");
    expect(cards.map((c) => c.textContent)).toEqual(["Good chart"]);
  });

  it("legacy rows without tool_call_id pair in FIFO order", async () => {
    const charts = [
      { id: null, title: "Legacy one", ok: true },
      { id: null, title: "Legacy two", ok: true },
    ];
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: batchRows(charts),
    });
    renderPanel();
    const cards = await screen.findAllByTestId("chart-proposal-card");
    expect(cards.map((c) => c.textContent)).toEqual(["Legacy one", "Legacy two"]);
  });
});
