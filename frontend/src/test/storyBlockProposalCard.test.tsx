import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StoryBlockProposalCard } from "@/components/agent/StoryBlockProposalCard";
import { agentApi, type AgentProposal } from "@/api/agent";
import { storiesApi } from "@/api/stories";
import type { Story } from "@/api/types";

const CASE = "c1";
const CONV = "conv1";

const STORY: Story = {
  id: "s1",
  case_id: CASE,
  title: "Intrusion report",
  description: null,
  created_by: "alice",
  updated_by: "alice",
  created_at: "2026-07-26T10:00:00+00:00",
  updated_at: "2026-07-26T10:00:00+00:00",
};

function proposal(overrides: Partial<AgentProposal> = {}): AgentProposal {
  return {
    id: "p1",
    conversation_id: CONV,
    case_id: CASE,
    timeline_id: "t1",
    status: "proposed",
    kind: "story_block",
    payload: {
      story_id: "s1",
      block_kind: "markdown",
      content: { text: "## Agent summary" },
      after_block_id: null,
    },
    tag: null,
    comment: null,
    rationale: "summarizes the brute-force window",
    events: [],
    created_at: null,
    decided_by: null,
    decided_at: null,
    result: null,
    ...overrides,
  };
}

function renderCard(p: AgentProposal) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <StoryBlockProposalCard caseId={CASE} conversationId={CONV} proposal={p} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(storiesApi, "list").mockResolvedValue([STORY]);
});

describe("StoryBlockProposalCard", () => {
  it("previews the drafted block and names its target story", async () => {
    renderCard(proposal());
    expect(await screen.findByText("Intrusion report")).toBeInTheDocument();
    expect(screen.getByText("Agent summary")).toBeInTheDocument();
    expect(screen.getByText(/summarizes the brute-force window/)).toBeInTheDocument();
  });

  it("confirm is the analyst's write — the card never writes on render", async () => {
    const confirm = vi
      .spyOn(agentApi, "confirmProposal")
      .mockResolvedValue({ proposal: proposal({ status: "confirmed" }), applied: true });
    renderCard(proposal());

    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(CASE, CONV, "p1"));
  });

  it("rejecting writes nothing and renders the decided state", async () => {
    const reject = vi
      .spyOn(agentApi, "rejectProposal")
      .mockResolvedValue({ proposal: proposal({ status: "rejected" }) });
    renderCard(proposal());

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(reject).toHaveBeenCalled());
  });

  it("says so plainly when the target story is gone", async () => {
    vi.spyOn(storiesApi, "list").mockResolvedValue([]);
    renderCard(proposal());
    expect(await screen.findByText(/no longer exists/)).toBeInTheDocument();
  });

  it("renders the decided state for an already-rejected proposal", () => {
    renderCard(proposal({ status: "rejected", decided_by: "u1" }));
    expect(screen.getByText(/Story block proposal rejected/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });
});
