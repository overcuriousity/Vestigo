/**
 * AgentPanel: persisted marker rows (`window`, plus historical `fidelity`)
 * render their notices after a reload. The live SSE events are gone once the
 * panel remounts, so without these branches the analyst would see a thinner
 * investigation with nothing in the transcript explaining it.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentPanel } from "@/components/agent/AgentPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAgentStore } from "@/stores/agent";
import type { AgentConversation, AgentMessage } from "@/api/agent";

beforeAll(() => {
  Element.prototype.scrollTo = vi.fn();
});

const listConversationsMock = vi.fn();
const getConversationMock = vi.fn();
const listProposalsMock = vi.fn();
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

function markerRow(role: "fidelity" | "window", toolResult: unknown): AgentMessage {
  return {
    id: "m1",
    conversation_id: CONV_ID,
    role,
    content: "Tool results did not fit the model's context window — reduced.",
    tool_name: null,
    tool_args: null,
    tool_result: toolResult,
    created_at: null,
  };
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

describe("AgentPanel fidelity marker rows", () => {
  it("renders the reduction notice with the tier the turn was re-run at", async () => {
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: [markerRow("fidelity", { from: "full", to: "message", attempt: 0, reason: "overflow" })],
    });
    renderPanel();
    expect(await screen.findByText(/retried with less detail per event \(message\)/)).toBeTruthy();
  });

  it("ignores a marker row with no tier — never invents one", async () => {
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: [markerRow("fidelity", null)],
    });
    renderPanel();
    await screen.findByTestId("agent-panel");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText(/retried with less detail/)).toBeNull();
  });
});

describe("AgentPanel window marker rows", () => {
  it("renders the elision notice with the turn's stats", async () => {
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: [
        markerRow("window", {
          reason: "fit",
          attempt: 0,
          budget: 3000,
          results_elided: 4,
          turns_dropped: 1,
          estimated_before: 5000,
          estimated_after: 2800,
        }),
      ],
    });
    renderPanel();
    expect(
      await screen.findByText(/elided to fit the model's context window \(4 elided, 1 turns dropped\)/),
    ).toBeTruthy();
  });

  it("names truncated results when the last-resort pass ran", async () => {
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: [
        markerRow("window", {
          reason: "fit",
          attempt: 0,
          budget: 3000,
          results_elided: 2,
          results_truncated: 1,
          turns_dropped: 0,
          estimated_before: 9000,
          estimated_after: 2900,
        }),
      ],
    });
    renderPanel();
    expect(await screen.findByText(/\(2 elided, 1 truncated\)/)).toBeTruthy();
  });

  it("renders the overflow-retry notice", async () => {
    getConversationMock.mockResolvedValue({
      ...conversation(),
      messages: [markerRow("window", { reason: "overflow", attempt: 0, budget: 2400 })],
    });
    renderPanel();
    expect(
      await screen.findByText(/exceeded the model's context window — retrying/),
    ).toBeTruthy();
  });
});
