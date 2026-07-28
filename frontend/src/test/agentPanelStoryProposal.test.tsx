/**
 * AgentPanel: a proposal tool must render its card on the *live* stream, not
 * only after a reload — the live path is the one an analyst actually watches.
 *
 * W7's `propose_story_block` shipped wired into one of the four render paths,
 * so it broke two separate ways:
 *
 *   1. `foldStreamEvent` had no branch for the tool, so the call row fell
 *      through to the generic tool row (a bare "propose_story_block" line)
 *      and the result row produced no card at all.
 *   2. The proposals query was only invalidated for `propose_annotation`, so
 *      even once the transcript refetch produced a `storyProposal` item, the
 *      proposal itself was missing from the (stale) list and the card fell
 *      back to the same bare tool row until the panel remounted.
 *
 * Both are now driven off `PROPOSAL_TOOLS`, so this suite is parameterized
 * over that map rather than over one tool: adding a proposal tool there is
 * what earns it live-stream coverage of all four paths, and a tool wired into
 * the map but not the folds fails here.
 *
 * The card components are mocked — their own data fetching is not under test.
 */
import { describe, it, expect, vi, beforeEach, afterEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { PROPOSAL_TOOLS, type ProposalItemKind } from "@/components/agent/proposalTools";
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

/** One testid per ChatItem kind, so a card rendered for the wrong kind fails. */
const CARD_TESTID: Record<ProposalItemKind, string> = {
  proposal: "annotation-proposal-card",
  storyProposal: "story-proposal-card",
};

vi.mock("@/components/agent/StoryBlockProposalCard", () => ({
  StoryBlockProposalCard: (props: { proposal: AgentProposal }) => (
    <div data-testid="story-proposal-card">{props.proposal.id}</div>
  ),
}));

// Mocked for the same reason as the proposal cards, plus it routes: the real
// one calls useNavigate and there is no Router here.
vi.mock("@/components/agent/FindingCard", () => ({
  FindingCard: (props: { title: string }) => (
    <div data-testid="finding-card">{props.title}</div>
  ),
}));

vi.mock("@/components/agent/ProposalCard", () => ({
  ProposalCard: (props: { proposal: AgentProposal }) => (
    <div data-testid="annotation-proposal-card">{props.proposal.id}</div>
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

/** A proposal of the kind the given ChatItem kind resolves against. */
function proposal(itemKind: ProposalItemKind): AgentProposal {
  const storyBlock = itemKind === "storyProposal";
  return {
    id: PROPOSAL_ID,
    conversation_id: CONV_ID,
    case_id: CASE,
    timeline_id: TL,
    status: "proposed",
    kind: storyBlock ? "story_block" : "annotation",
    payload: storyBlock
      ? {
          story_id: "story1",
          block_kind: "markdown",
          content: { text: "Source IP country analysis" },
          after_block_id: null,
        }
      : null,
    tag: storyBlock ? null : "suspicious",
    comment: storyBlock ? null : "unusual country spread",
    rationale: "key findings",
    events: [],
    created_at: null,
    decided_by: null,
    decided_at: null,
  };
}

/** The SSE events a successful proposal turn emits for the given tool. */
function turnEvents(tool: string): AgentStreamEvent[] {
  return [
    {
      type: "tool_call",
      tool,
      tool_call_id: "tc1",
      args: { story_id: "story1", block_kind: "markdown", content: { text: "…" } },
    },
    {
      type: "tool_result",
      tool,
      tool_call_id: "tc1",
      result: { proposal_id: PROPOSAL_ID },
    },
  ] satisfies AgentStreamEvent[];
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

/** Type a message and send it, driving the mocked stream. */
async function sendTurn() {
  const input = await screen.findByPlaceholderText(/What should the agent look into/);
  fireEvent.change(input, { target: { value: "summarize the country spread" } });
  fireEvent.keyDown(input, { key: "Enter" });
}

describe.each(Object.entries(PROPOSAL_TOOLS))(
  "AgentPanel %s (live stream)",
  (tool, itemKind) => {
    const testId = CARD_TESTID[itemKind];
    // The panel drops its live items once the turn ends and the persisted
    // transcript takes over, so a turn that completes instantly would let
    // these assertions pass off the *reload* path — the one that was never
    // broken. Hold the stream open past the events instead.
    let releaseTurn = () => {};

    beforeEach(() => {
      vi.clearAllMocks();
      useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
      listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
      getConversationMock.mockResolvedValue({ ...conversation(), messages: [] });
      // The panel's first fetch predates the proposal — as it does in a real
      // turn. Only the invalidation-driven refetch can resolve the card.
      listProposalsMock
        .mockResolvedValueOnce({ proposals: [] })
        .mockResolvedValue({ proposals: [proposal(itemKind)] });
      getInfoMock.mockResolvedValue({
        api_base_url: "https://llm.example",
        model: "test-model",
        tools: [{ name: tool, description: "", admin_disabled: false }],
        user_disabled_tools: [],
      });
      const held = new Promise<void>((resolve) => {
        releaseTurn = resolve;
      });
      streamMessageMock.mockImplementation(
        async (
          _case: string,
          _conv: string,
          _body: unknown,
          onEvent: (e: AgentStreamEvent) => void,
        ) => {
          for (const e of turnEvents(tool)) onEvent(e);
          await held;
        },
      );
    });

    // Release before RTL's cleanup so the stream's continuation (a setState
    // and a query invalidation) lands while the panel is still mounted,
    // rather than as an act warning attributed to whatever test runs next.
    afterEach(async () => {
      await act(async () => {
        releaseTurn();
      });
    });

    it("renders one proposal card while the turn streams", async () => {
      renderPanel();
      await waitFor(() => expect(listProposalsMock).toHaveBeenCalled());
      await sendTurn();

      const cards = await screen.findAllByTestId(testId);
      expect(cards).toHaveLength(1);
      expect(cards[0]).toHaveTextContent(PROPOSAL_ID);
    });

    it("does not also render the raw tool row for the call", async () => {
      // The generic fallback row is what the analyst saw instead of the card.
      renderPanel();
      await sendTurn();

      await screen.findAllByTestId(testId);
      // queryAllBy, not queryBy: a regression renders the row more than once
      // and queryBy would throw "found multiple elements" instead of failing
      // this assertion.
      expect(screen.queryAllByText(new RegExp(tool))).toHaveLength(0);
    });

    it("refetches the proposals list so the card can resolve its proposal", async () => {
      // Without the invalidation the list stays the one fetched *before* the
      // proposal existed, and the card falls back to a bare tool row — which
      // is why the mock above returns an empty list first.
      renderPanel();
      await waitFor(() => expect(listProposalsMock).toHaveBeenCalled());
      const before = listProposalsMock.mock.calls.length;

      await sendTurn();
      await waitFor(() => expect(listProposalsMock.mock.calls.length).toBeGreaterThan(before));
    });

    it("falls back to the tool row when the proposal is of another kind", async () => {
      // Two item kinds now resolve against one query. A card handed the other
      // shape reads its payload off fields that are null there, so the guard
      // degrades to the same row a missing proposal gets.
      const otherKind: ProposalItemKind = itemKind === "proposal" ? "storyProposal" : "proposal";
      listProposalsMock.mockReset();
      listProposalsMock.mockResolvedValue({ proposals: [proposal(otherKind)] });

      renderPanel();
      await sendTurn();

      await waitFor(() => expect(screen.getAllByText(new RegExp(tool)).length).toBeGreaterThan(0));
      expect(screen.queryAllByTestId(testId)).toHaveLength(0);
    });
  },
);

// The other direction: propose_finding renders from its *call* args and never
// touches the proposals query, so widening PROPOSAL_TOOLS to CARD_TOOLS would
// break its card. Nothing else pins that, and the suite would stay green.
describe("AgentPanel propose_finding (live stream)", () => {
  let releaseTurn = () => {};

  beforeEach(() => {
    vi.clearAllMocks();
    useAgentStore.getState().setActiveConversation(`${CASE}/${TL}`, CONV_ID);
    listConversationsMock.mockResolvedValue({ conversations: [conversation()] });
    getConversationMock.mockResolvedValue({ ...conversation(), messages: [] });
    listProposalsMock.mockResolvedValue({ proposals: [] });
    getInfoMock.mockResolvedValue({
      api_base_url: "https://llm.example",
      model: "test-model",
      tools: [{ name: "propose_finding", description: "", admin_disabled: false }],
      user_disabled_tools: [],
    });
    const held = new Promise<void>((resolve) => {
      releaseTurn = resolve;
    });
    streamMessageMock.mockImplementation(
      async (
        _case: string,
        _conv: string,
        _body: unknown,
        onEvent: (e: AgentStreamEvent) => void,
      ) => {
        onEvent({
          type: "tool_call",
          tool: "propose_finding",
          tool_call_id: "tc1",
          args: { title: "Rare country", description: "one host", filters: {} },
        });
        onEvent({
          type: "tool_result",
          tool: "propose_finding",
          tool_call_id: "tc1",
          result: { proposal_id: PROPOSAL_ID },
        });
        await held;
      },
    );
  });

  afterEach(async () => {
    await act(async () => {
      releaseTurn();
    });
  });

  it("renders no proposal card and does not refetch the proposals list", async () => {
    renderPanel();
    await waitFor(() => expect(listProposalsMock).toHaveBeenCalled());
    const before = listProposalsMock.mock.calls.length;

    await sendTurn();
    await screen.findByText("Rare country");

    for (const id of Object.values(CARD_TESTID)) {
      expect(screen.queryAllByTestId(id)).toHaveLength(0);
    }
    expect(listProposalsMock.mock.calls.length).toBe(before);
  });
});
