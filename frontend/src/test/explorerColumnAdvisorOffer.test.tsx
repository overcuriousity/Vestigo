/**
 * The one-time AI column offer on ExplorerPage (issue #213 follow-up).
 *
 * A timeline always opens on locally-scored columns; whether the model gets to
 * re-rank them is a question only the analyst can answer. That question used to
 * live exclusively behind a button inside the Columns popover, so the common
 * path — create a timeline, open it — landed on the heuristic answer with
 * nothing on screen saying a better one existed. A disclosure nobody finds is
 * not a choice anybody made.
 *
 * So it is offered once per timeline, on first open, to whoever can act on it.
 * What matters here is that the *gates* hold: no offer without a model, without
 * contribute access, without a local suggestion to re-rank, or to someone who
 * already answered — and, critically, nothing is sent by the offer merely
 * appearing.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAuthStore } from "@/stores/auth";
import { useAgentStore } from "@/stores/agent";
import { useScrollPositionStore } from "@/stores/scrollPosition";
import type { Case, EventPage, RecommendedColumns, User } from "@/api/types";

const recommendColumnsMock = vi.fn();
const updatePreferencesMock = vi.fn();
const caseGetMock = vi.fn();
const timelineGetMock = vi.fn();
const capabilities = { agent: true };

vi.mock("@/api/events", async () => {
  const actual = await vi.importActual<typeof import("@/api/events")>("@/api/events");
  return {
    ...actual,
    eventsApi: {
      ...actual.eventsApi,
      list: async () => PAGE,
      mergedTags: async () => [],
      artifacts: async () => [],
      fields: async () => ({ top_level: [], attributes: [] }),
    },
  };
});
vi.mock("@/api/dispositions", async () => {
  const actual = await vi.importActual<typeof import("@/api/dispositions")>("@/api/dispositions");
  return { ...actual, dispositionsApi: { ...actual.dispositionsApi, list: async () => [] } };
});
vi.mock("@/api/annotations", async () => {
  const actual = await vi.importActual<typeof import("@/api/annotations")>("@/api/annotations");
  return {
    ...actual,
    annotationsApi: {
      ...actual.annotationsApi,
      listForTimeline: async () => [],
      listDistinctTags: async () => [],
    },
  };
});
vi.mock("@/api/timelines", async () => {
  const actual = await vi.importActual<typeof import("@/api/timelines")>("@/api/timelines");
  return {
    ...actual,
    timelinesApi: {
      ...actual.timelinesApi,
      get: (...args: unknown[]) => timelineGetMock(...args),
      listSources: async () => [],
      recommendColumns: (...args: unknown[]) => recommendColumnsMock(...args),
    },
  };
});
vi.mock("@/api/cases", async () => {
  const actual = await vi.importActual<typeof import("@/api/cases")>("@/api/cases");
  return { ...actual, casesApi: { ...actual.casesApi, get: (...a: unknown[]) => caseGetMock(...a) } };
});
vi.mock("@/api/auth", async () => {
  const actual = await vi.importActual<typeof import("@/api/auth")>("@/api/auth");
  return {
    ...actual,
    authApi: { ...actual.authApi, updatePreferences: (...a: unknown[]) => updatePreferencesMock(...a) },
  };
});
vi.mock("@/api/views", async () => {
  const actual = await vi.importActual<typeof import("@/api/views")>("@/api/views");
  return { ...actual, viewsApi: { ...actual.viewsApi, list: async () => [] } };
});
vi.mock("@/api/baselines", async () => {
  const actual = await vi.importActual<typeof import("@/api/baselines")>("@/api/baselines");
  return { ...actual, baselinesApi: { ...actual.baselinesApi, list: async () => ({ baselines: [] }) } };
});
vi.mock("@/api/agent", async () => {
  const actual = await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    agentApi: {
      ...actual.agentApi,
      getInfo: async () => ({
        model: "qwen3-coder",
        provider: "openai",
        api_base_url: "http://10.0.0.4:8000/v1",
        context_window: null,
        tools: [],
        user_disabled_tools: [],
      }),
    },
  };
});
vi.mock("@/api/health", () => ({
  useHealth: () => ({ data: { agent_available: true } }),
  useCapabilities: () => capabilities,
  useAnnotatedTag: () => "annotated",
}));
vi.mock("@/hooks/useCaseStream", () => ({ useCaseStream: () => undefined }));

// Presentational children stubbed — the subject is the offer's gating.
vi.mock("@/components/explorer/EventGrid", () => ({ EventGrid: () => null }));
vi.mock("@/components/explorer/TimelineHistogram", () => ({ TimelineHistogram: () => null }));
vi.mock("@/components/explorer/FilterRail", () => ({ FilterRail: () => null }));
vi.mock("@/components/explorer/FilterChips", () => ({ FilterChips: () => null }));
vi.mock("@/components/explorer/EventDetailPanel", () => ({ EventDetailPanel: () => null }));
vi.mock("@/components/analysis/InvestigatePanel", () => ({ InvestigatePanel: () => null }));
vi.mock("@/components/agent/AgentPanel", () => ({ AgentPanel: () => null }));
vi.mock("@/components/viz/FieldHistogramModal", () => ({ FieldHistogramModal: () => null }));

import { ExplorerPage } from "@/pages/ExplorerPage";

const PAGE: EventPage = {
  total: 0,
  offset: 0,
  limit: 100,
  events: [],
  has_more_after: false,
  has_more_before: false,
  next_cursor: null,
  prev_cursor: null,
  routine_collapsed_count: 0,
};

/** A finished, locally-scored suggestion — the state the offer exists for. */
const HEURISTIC: RecommendedColumns = {
  status: "ok",
  columns: ["timestamp", "src_ip"],
  reasons: { src_ip: "in 2/2 sources · 98% filled · 41 distinct values" },
  method: "heuristic",
  model: null,
  source_ids: ["s1"],
  generated_at: "2026-07-31T00:00:00Z",
  job_id: null,
};

function testUser(over: Partial<User> = {}): User {
  return {
    id: "u1",
    username: "alice",
    display_name: null,
    email: null,
    is_admin: false,
    is_active: true,
    must_change_password: false,
    auth_provider: "local",
    onboarding_completed: true,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    last_login_at: null,
    preferences: null,
    ...over,
  };
}

function testCase(accessLevel: Case["access_level"]): Case {
  return { id: "c1", name: "C1", access_level: accessLevel } as Case;
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/cases/c1/timelines/t1"]}>
          <Routes>
            <Route path="/cases/:caseId/timelines/:timelineId" element={<ExplorerPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  capabilities.agent = true;
  recommendColumnsMock.mockReset().mockResolvedValue({ job_id: "job-1", use_ai: true });
  updatePreferencesMock.mockReset();
  caseGetMock.mockReset().mockResolvedValue(testCase("contribute"));
  timelineGetMock.mockReset().mockResolvedValue({
    id: "t1",
    case_id: "c1",
    name: "T1",
    source_ids: ["s1"],
    recommended_columns: HEURISTIC,
  });
  useAuthStore.setState({ user: testUser() });
  useScrollPositionStore.getState().setCurrentPositionTs(null);
  useAgentStore.getState().setPanelOpen(false);
});

describe("ExplorerPage column advisor offer", () => {
  it("offers once on a timeline nobody has answered for", async () => {
    renderPage();

    expect(await screen.findByText("Suggest columns with AI")).toBeInTheDocument();
    // Appearing is not consenting: nothing has been sent or recorded yet.
    expect(recommendColumnsMock).not.toHaveBeenCalled();
    expect(updatePreferencesMock).not.toHaveBeenCalled();
  });

  it("records the opt-in and runs the AI suggestion on confirm", async () => {
    updatePreferencesMock.mockResolvedValue(
      testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    await waitFor(() =>
      expect(updatePreferencesMock).toHaveBeenCalledWith({
        column_advisor_optin: { t1: true },
      }),
    );
    await waitFor(() => expect(recommendColumnsMock).toHaveBeenCalledWith("c1", "t1", true));
  });

  it("records a dismissal as an answer, so the offer does not come back", async () => {
    // Without this the offer returns on every visit, which is how people learn
    // to dismiss a consent dialog unread.
    updatePreferencesMock.mockResolvedValue(
      testUser({ preferences: { column_advisor_optin: { t1: false } } }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));

    await waitFor(() =>
      expect(updatePreferencesMock).toHaveBeenCalledWith({
        column_advisor_optin: { t1: false },
      }),
    );
    expect(recommendColumnsMock).not.toHaveBeenCalled();
  });

  it("stays quiet for a timeline the analyst already declined", async () => {
    useAuthStore.setState({
      user: testUser({ preferences: { column_advisor_optin: { t1: false } } }),
    });
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });

  it("stays quiet for a timeline the analyst already opted in to", async () => {
    useAuthStore.setState({
      user: testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    });
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });

  it("stays quiet with no model configured", async () => {
    capabilities.agent = false;
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });

  it("stays quiet for a read-only member, who could not act on the answer", async () => {
    // The result is shared with everyone who can see the timeline, so it is a
    // contribute action — offering it to a reader is offering a button that
    // would 403.
    caseGetMock.mockResolvedValue(testCase("read"));
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });

  it("stays quiet when the suggestion is already the model's", async () => {
    timelineGetMock.mockResolvedValue({
      id: "t1",
      case_id: "c1",
      name: "T1",
      source_ids: ["s1"],
      recommended_columns: { ...HEURISTIC, method: "llm", model: "qwen3-coder" },
    });
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });

  it("stays quiet while there is no local suggestion to re-rank yet", async () => {
    timelineGetMock.mockResolvedValue({
      id: "t1",
      case_id: "c1",
      name: "T1",
      source_ids: ["s1"],
      recommended_columns: null,
    });
    renderPage();

    await waitFor(() => expect(caseGetMock).toHaveBeenCalled());
    expect(screen.queryByText("Suggest columns with AI")).toBeNull();
  });
});
