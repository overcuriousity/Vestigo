/**
 * AgentPanel (#205): a persistent bar shows the Explorer filters the agent
 * inherits as per-message context — read-only, live-tracking, with an empty
 * state when the agent sees the whole timeline.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAgentStore } from "@/stores/agent";
import type { AgentConversation, AgentMessage } from "@/api/agent";
import type { EventFilters } from "@/api/types";

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

const ANCHOR: AgentMessage = {
  id: "anchor",
  conversation_id: CONV_ID,
  role: "assistant",
  content: "transcript rendered",
  tool_name: null,
  tool_args: null,
  tool_result: null,
  created_at: null,
} as AgentMessage;

function panelTree(currentFilters: EventFilters) {
  return (
    <TooltipProvider>
      <AgentPanel
        caseId={CASE}
        timelineId={TL}
        currentFilters={currentFilters}
        onApplyFilters={vi.fn()}
        onClose={vi.fn()}
      />
    </TooltipProvider>
  );
}

function renderPanel(currentFilters: EventFilters) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={qc}>{panelTree(currentFilters)}</QueryClientProvider>,
  );
  return {
    ...result,
    rerenderPanel: (next: EventFilters) =>
      result.rerender(
        <QueryClientProvider client={qc}>{panelTree(next)}</QueryClientProvider>,
      ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
  listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
  listProposalsMock.mockResolvedValue({ proposals: [] });
  getConversationMock.mockResolvedValue({ ...conversation(), messages: [ANCHOR] });
});

describe("agent panel inherited-filters bar (#205)", () => {
  it("shows the live Explorer filters the agent inherits", async () => {
    renderPanel({ q: "ssh", tagsInclude: ["auth"] });
    const bar = await screen.findByTestId("agent-inherited-filters");
    expect(bar.textContent).toContain("ssh");
    expect(bar.textContent).toContain("auth");
    // Read-only: no remove buttons inside the bar.
    expect(bar.querySelector("button")).toBeNull();
  });

  it("says when the agent sees the whole timeline", async () => {
    renderPanel({});
    const bar = await screen.findByTestId("agent-inherited-filters");
    expect(bar.textContent).toContain("whole timeline");
  });

  it("tracks live filter changes (the next message inherits them)", async () => {
    const { rerenderPanel } = renderPanel({ q: "ssh" });
    const bar = await screen.findByTestId("agent-inherited-filters");
    expect(bar.textContent).toContain("ssh");
    rerenderPanel({ q: "dns" });
    expect(bar.textContent).toContain("dns");
    expect(bar.textContent).not.toContain("ssh");
  });
});
