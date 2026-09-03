/**
 * StoryProposalCard: what the transcript claims a confirm did.
 *
 * A confirm can decide the proposal and create nothing — the title was taken
 * between propose and confirm. The card used to render the success state for
 * that case anyway, and its "Open story" button resolved the story *by
 * title*, so the transcript permanently read "created by <analyst>" and
 * linked to the unrelated story that took the name. The toast saying
 * otherwise is gone by the next page load; the persisted `result` is not.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StoryProposalCard } from "@/components/agent/StoryProposalCard";
import { type AgentProposal } from "@/api/agent";
import { storiesApi } from "@/api/stories";
import type { Story } from "@/api/types";

const CASE = "c1";
const CONV = "conv1";

const STORY: Story = {
  id: "s1",
  case_id: CASE,
  title: "Abschlussbericht",
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
    status: "confirmed",
    kind: "story",
    payload: { title: "Abschlussbericht", description: null },
    tag: null,
    comment: null,
    rationale: "the case has no report yet",
    events: [],
    created_at: null,
    decided_by: "alice",
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
        <StoryProposalCard caseId={CASE} conversationId={CONV} proposal={p} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(storiesApi, "list").mockResolvedValue([STORY]);
});

describe("StoryProposalCard", () => {
  it("does not claim a story it did not create", async () => {
    renderCard(
      proposal({
        result: {
          applied: false,
          story_id: null,
          reason: "a story titled 'Abschlussbericht' already exists",
        },
      }),
    );

    expect(await screen.findByText(/no story was created/)).toBeInTheDocument();
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
    // The story that took the name is not offered as if it were the agent's.
    expect(screen.queryByRole("link", { name: "Open story" })).not.toBeInTheDocument();
  });

  it("links the story it did create by id, not by title", async () => {
    renderCard(proposal({ result: { applied: true, story_id: "s1", reason: null } }));

    expect(await screen.findByText(/created by alice/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Open story" })).toHaveAttribute(
        "href",
        `/cases/${CASE}/stories/s1`,
      ),
    );
  });

  it("falls back to the title for a row confirmed before outcomes were recorded", async () => {
    renderCard(proposal());
    expect(await screen.findByText(/created by alice/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Open story" })).toHaveAttribute(
        "href",
        `/cases/${CASE}/stories/s1`,
      ),
    );
  });
});
