/**
 * AgentPanel (W7): a `propose_story_block` call must render its
 * StoryBlockProposalCard on the *live* stream, not only after a reload — the
 * live path is the one an analyst actually watches. Two separate ways that
 * broke, both covered here:
 *
 *   1. `foldStreamEvent` had no branch for the tool, so the call row fell
 *      through to the generic tool row (a bare "propose_story_block" line)
 *      and the result row produced no card at all.
 *   2. The proposals query was only invalidated for `propose_annotation`, so
 *      even once the transcript refetch produced a `storyProposal` item, the
 *      proposal itself was missing from the (stale) list and the card fell
 *      back to the same bare tool row until the panel remounted.
 *
 * StoryBlockProposalCard is mocked — its own data fetching is not what's
 * under test here.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAgentStore } from "@/stores/agent";
import type { AgentConversation, AgentProposal, AgentStreamEvent } from "@/api/agent";

beforeAll(() => {
  Element.prototype.scrollTo = vi.fn();
});

const listConversationsMock = vi.fn();
const getConversationMock = vi.fn();
const listProposalsMock = vi.fn();
const streamMessageMock = vi.fn();
const getInfoMock = vi.fn();

vi.mock("@/api/agent", async () => {
  const actual = await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    agentApi: {
      listConversations: (...a: unknown[]) => listConversationsMock(...a),
      getConversation: (...a: unknown[]) => getConversationMock(...a),
      listProposals: (...a: unknown[]) => listProposalsMock(...a),
      streamMessage: (...a: unknown[]) => streamMessageMock(...a),
      getInfo: (...a: unknown[]) => getInfoMock(...a),
    },
  };
});

vi.mock("@/components/agent/StoryBlockProposalCard", () => ({
  StoryBlockProposalCard: (props: { proposal: AgentProposal }) => (
    <div data-testid="story-proposal-card">{props.proposal.id}</div>
  ),
}));

const CASE = "c1";
const TL = "t1";
const CONV_ID = "conv1";
const PROPOSAL_ID = "agentprop_1";

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

function storyProposal(): AgentProposal {
  return {
    id: PROPOSAL_ID,
    conversation_id: CONV_ID,
    case_id: CASE,
    timeline_id: TL,
    status: "proposed",
    kind: "story_block",
    payload: {
      story_id: "story1",
      block_kind: "markdown",
      content: { text: "Source IP country analysis" },
      after_block_id: null,
    },
    tag: null,
    comment: null,
    rationale: "key findings",
    events: [],
    created_at: null,
    decided_by: null,
    decided_at: null,
  };
}

/** The SSE events a successful propose_story_block turn emits. */
const TURN_EVENTS: AgentStreamEvent[] = [
  {
    type: "tool_call",
    tool: "propose_story_block",
    tool_call_id: "tc1",
    args: { story_id: "story1", block_kind: "markdown", content: { text: "…" } },
  } as AgentStreamEvent,
  {
    type: "tool_result",
    tool: "propose_story_block",
    tool_call_id: "tc1",
    result: { proposal_id: PROPOSAL_ID },
  } as AgentStreamEvent,
];

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

/** Type a message and send it, driving the mocked stream. */
async function sendTurn() {
  const input = await screen.findByPlaceholderText(/What should the agent look into/);
  fireEvent.change(input, { target: { value: "summarize the country spread" } });
  fireEvent.keyDown(input, { key: "Enter" });
}

beforeEach(() => {
  vi.clearAllMocks();
  useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
  listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
  getConversationMock.mockResolvedValue({ ...conversation(), messages: [] });
  listProposalsMock.mockResolvedValue({ proposals: [storyProposal()] });
  getInfoMock.mockResolvedValue({
    api_base_url: "https://llm.example",
    model: "test-model",
    tools: [{ name: "propose_story_block", description: "", admin_disabled: false }],
    user_disabled_tools: [],
  });
  streamMessageMock.mockImplementation(
    async (
      _case: string,
      _conv: string,
      _body: unknown,
      onEvent: (e: AgentStreamEvent) => void,
    ) => {
      for (const e of TURN_EVENTS) onEvent(e);
    },
  );
});

describe("AgentPanel propose_story_block (live stream)", () => {
  it("renders one StoryBlockProposalCard while the turn streams", async () => {
    renderPanel();
    await sendTurn();

    const cards = await screen.findAllByTestId("story-proposal-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent(PROPOSAL_ID);
  });

  it("does not also render the raw tool row for the call", async () => {
    // The generic fallback row is what the analyst saw instead of the card.
    renderPanel();
    await sendTurn();

    await screen.findAllByTestId("story-proposal-card");
    expect(screen.queryByText(/propose_story_block/)).toBeNull();
  });

  it("refetches the proposals list so the card can resolve its proposal", async () => {
    // Without the invalidation the list is the one fetched *before* the
    // proposal existed, and the card falls back to a bare tool row.
    renderPanel();
    await waitFor(() => expect(listProposalsMock).toHaveBeenCalled());
    const before = listProposalsMock.mock.calls.length;

    await sendTurn();
    await waitFor(() => expect(listProposalsMock.mock.calls.length).toBeGreaterThan(before));
  });
});
