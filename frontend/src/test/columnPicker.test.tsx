/**
 * ColumnPicker derived-key grouping (PR #54 finding #34): enrichment-derived
 * keys collapse under their parent attribute, search auto-expands them, and
 * selection always uses the full raw key.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ColumnPicker } from "@/components/explorer/ColumnPicker";
import { useUiStore } from "@/stores/ui";
import { useAuthStore } from "@/stores/auth";
import type { RecommendedColumns, User } from "@/api/types";

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

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    recommendColumns: vi.fn().mockResolvedValue({ job_id: "job-1", use_ai: false }),
  },
}));

vi.mock("@/api/auth", () => ({
  authApi: { updatePreferences: vi.fn() },
}));

vi.mock("@/api/agent", () => ({
  agentApi: {
    getInfo: vi.fn().mockResolvedValue({
      model: "qwen3-coder",
      provider: "openai",
      api_base_url: "http://10.0.0.4:8000/v1",
      context_window: null,
      tools: [],
      user_disabled_tools: [],
    }),
  },
}));

const capabilities = { agent: false };
vi.mock("@/api/health", () => ({
  useCapabilities: () => capabilities,
}));

vi.mock("@/api/events", () => ({
  eventsApi: {
    fields: vi.fn().mockResolvedValue({
      top_level: ["timestamp", "message"],
      attributes: [
        "dst_ip",
        "src_ip",
        "src_ip:geo_city",
        "src_ip:geo_country",
        "zulu:geo_country",
      ],
      derived_suffixes: ["geo_city", "geo_country"],
      mapped: [],
    }),
  },
}));

async function renderOpenPicker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ColumnPicker caseId="c1" timelineId="t1" />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /columns/i }));
  await waitFor(() => expect(screen.getByText("src_ip")).toBeInTheDocument());
}

beforeEach(async () => {
  useUiStore.setState({ visibleColumnsByTimeline: {} });
  capabilities.agent = false;
  // Both are module-level mocks, so a rejection armed by one test would
  // otherwise be the next test's default.
  const { timelinesApi } = await import("@/api/timelines");
  vi.mocked(timelinesApi.recommendColumns).mockReset();
  vi.mocked(timelinesApi.recommendColumns).mockResolvedValue({
    job_id: "job-1",
    use_ai: false,
  });
  const { authApi } = await import("@/api/auth");
  vi.mocked(authApi.updatePreferences).mockReset();
});

describe("ColumnPicker derived-key grouping", () => {
  it("collapses derived keys under their parent attribute", async () => {
    await renderOpenPicker();

    expect(screen.getByText("dst_ip")).toBeInTheDocument();
    expect(screen.getByText("Derived (2)")).toBeInTheDocument();
    // Collapsed by default: children hidden.
    expect(screen.queryByText("geo_city")).not.toBeInTheDocument();
    expect(screen.queryByText("src_ip:geo_country")).not.toBeInTheDocument();
  });

  it("expands on click and labels children by output field", async () => {
    await renderOpenPicker();

    fireEvent.click(screen.getByText("Derived (2)"));
    expect(screen.getByText("geo_city")).toBeInTheDocument();
    expect(screen.getByText("geo_country")).toBeInTheDocument();
  });

  it("puts derived keys without a listed parent into a trailing group", async () => {
    await renderOpenPicker();

    expect(screen.getByText("Derived fields")).toBeInTheDocument();
    expect(screen.getByText("zulu:geo_country")).toBeInTheDocument();
  });

  it("search auto-expands matching derived children", async () => {
    await renderOpenPicker();

    fireEvent.change(screen.getByPlaceholderText("Search fields…"), {
      target: { value: "geo_city" },
    });
    // Child visible without manual expansion; parent row kept visible too.
    expect(screen.getByText("geo_city")).toBeInTheDocument();
    expect(screen.getByText("src_ip")).toBeInTheDocument();
    // Unrelated base attribute filtered out.
    expect(screen.queryByText("dst_ip")).not.toBeInTheDocument();
  });

  it("selecting a derived child stores the full raw key", async () => {
    await renderOpenPicker();

    fireEvent.click(screen.getByText("Derived (2)"));
    const row = screen.getByText("geo_country").closest("label")!;
    fireEvent.click(row.querySelector("input")!);

    expect(useUiStore.getState().visibleColumnsByTimeline["c1/t1"]).toContain(
      "src_ip:geo_country",
    );
  });

  it("does not misclassify a raw vendor key whose colon suffix isn't a registered enricher output", async () => {
    // Regression: splitDerivedKey used to split on any colon, so a raw key
    // like "event_data:AccountName" (Windows Event Log/Sysmon-style) was
    // wrongly treated as enrichment-derived. It must only group under a
    // parent when the suffix is a known enricher output field.
    const { eventsApi } = await import("@/api/events");
    vi.mocked(eventsApi.fields).mockResolvedValueOnce({
      top_level: ["timestamp", "message"],
      attributes: ["event_data", "event_data:AccountName"],
      derived_suffixes: ["geo_city", "geo_country"],
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ColumnPicker caseId="c1" timelineId="t1" />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /columns/i }));
    await waitFor(() => expect(screen.getByText("event_data")).toBeInTheDocument());

    expect(screen.getByText("event_data:AccountName")).toBeInTheDocument();
    expect(screen.queryByText(/^Derived \(/)).not.toBeInTheDocument();
  });
});

/**
 * The suggestion surface (issue #213): a suggested column is marked with its
 * evidence, resetting clears the local override rather than writing one, and
 * recomputing is contribute-gated. The AI path additionally needs a configured
 * agent and this analyst's per-timeline opt-in.
 */
const SUGGESTION: RecommendedColumns = {
  status: "ok",
  columns: ["timestamp", "src_ip"],
  reasons: { src_ip: "in 2/2 sources · 98% filled · 41 distinct values" },
  method: "heuristic",
  model: null,
  source_ids: ["s1"],
  generated_at: "2026-07-31T00:00:00Z",
  job_id: null,
};

async function renderWithSuggestion(props: {
  recommended?: RecommendedColumns | null;
  canRecommend?: boolean;
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ColumnPicker caseId="c1" timelineId="t1" {...props} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /columns/i }));
  await waitFor(() => expect(screen.getByText("src_ip")).toBeInTheDocument());
}

describe("ColumnPicker attention flash", () => {
  it("announces itself when a timeline opens, and stops on its own", async () => {
    vi.useFakeTimers();
    try {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={qc}>
          <ColumnPicker caseId="c1" timelineId="t1" />
        </QueryClientProvider>,
      );

      const button = screen.getByRole("button", { name: /columns/i });
      expect(button.className).toContain("attention-pulse");

      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(button.className).not.toContain("attention-pulse");
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops as soon as the picker is opened", async () => {
    // The pulse means "you have not looked in here". Once someone has, it is
    // just a blinking button.
    await renderOpenPicker();

    expect(
      screen.getByRole("button", { name: /columns/i }).className,
    ).not.toContain("attention-pulse");
  });
});

describe("ColumnPicker suggestions", () => {
  it("marks suggested columns with the evidence behind them", async () => {
    await renderWithSuggestion({ recommended: SUGGESTION });

    expect(
      screen.getByLabelText(/Suggested column: in 2\/2 sources/),
    ).toBeInTheDocument();
  });

  it("ticks the suggested columns when the analyst has not chosen any", async () => {
    await renderWithSuggestion({ recommended: SUGGESTION });

    const row = screen.getByText("src_ip").closest("label")!;
    expect(row.querySelector("input")!).toBeChecked();
  });

  it("resetting clears the local override rather than writing one", async () => {
    useUiStore.setState({ visibleColumnsByTimeline: { "c1/t1": ["message"] } });
    await renderWithSuggestion({ recommended: SUGGESTION });

    fireEvent.click(screen.getByRole("button", { name: /reset to defaults/i }));

    // Absent, not equal-to-the-suggestion or to DEFAULT_COLUMNS: writing
    // either would opt this browser out of every later recomputation.
    expect(useUiStore.getState().visibleColumnsByTimeline).not.toHaveProperty("c1/t1");
  });

  it("still offers re-suggesting when the timeline has no suggestion yet", async () => {
    await renderWithSuggestion({ recommended: null, canRecommend: true });

    expect(screen.getByRole("button", { name: /re-suggest columns/i })).toBeInTheDocument();
  });

  it("hides the re-suggest action without contribute access", async () => {
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: false });

    expect(
      screen.queryByRole("button", { name: /re-suggest columns/i }),
    ).not.toBeInTheDocument();
  });

  it("re-suggesting starts a local job and tracks it in the tray", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    const { useJobsStore } = await import("@/stores/jobs");
    useJobsStore.setState({ jobs: {} });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));

    await waitFor(() => expect(useJobsStore.getState().jobs).toHaveProperty("job-1"));
    // `false`, explicitly: the plain button must never opt anyone into egress.
    expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1", false);
  });

  it("offers no AI path when no agent endpoint is configured", async () => {
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    expect(screen.queryByRole("button", { name: /suggest with ai/i })).not.toBeInTheDocument();
  });

  it("discloses before sending anything the first time on a timeline", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    capabilities.agent = true;
    useAuthStore.setState({ user: testUser() });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));

    expect(await screen.findByText("Suggest columns with AI")).toBeInTheDocument();
    expect(vi.mocked(timelinesApi.recommendColumns)).not.toHaveBeenCalledWith("c1", "t1", true);
  });

  it("records the opt-in and runs with AI once the disclosure is confirmed", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    const { authApi } = await import("@/api/auth");
    capabilities.agent = true;
    useAuthStore.setState({ user: testUser() });
    vi.mocked(authApi.updatePreferences).mockResolvedValue(
      testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    );
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));
    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    await waitFor(() =>
      expect(vi.mocked(authApi.updatePreferences)).toHaveBeenCalledWith({
        column_advisor_optin: { t1: true },
      }),
    );
    await waitFor(() =>
      expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1", true),
    );
  });

  it("skips the disclosure on a timeline this analyst already opted in to", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    capabilities.agent = true;
    useAuthStore.setState({
      user: testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));

    await waitFor(() =>
      expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1", true),
    );
    expect(screen.queryByText("Suggest columns with AI")).not.toBeInTheDocument();
  });

  it("reports a lost opt-in as lost, even after an unrelated failed re-suggest", async () => {
    // The two halves of the confirm are told apart by the confirm itself, not
    // by whether *some* recommend call has ever failed. Getting this backwards
    // tells the analyst their choice was saved when it was not, and the
    // disclosure never comes back to ask again.
    const { timelinesApi } = await import("@/api/timelines");
    const { authApi } = await import("@/api/auth");
    capabilities.agent = true;
    useAuthStore.setState({ user: testUser() });
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValueOnce(new Error("boom"));
    vi.mocked(authApi.updatePreferences).mockRejectedValue(new Error("nope"));
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    // The unrelated local run that fails first.
    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));
    await waitFor(() =>
      expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1", false),
    );

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));
    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    expect(await screen.findByText(/did not save/i)).toBeInTheDocument();
  });

  it("does not report a saved opt-in as lost when only the run failed", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    const { authApi } = await import("@/api/auth");
    capabilities.agent = true;
    useAuthStore.setState({ user: testUser() });
    vi.mocked(authApi.updatePreferences).mockResolvedValue(
      testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    );
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValue(new Error("boom"));
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));
    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    expect(await screen.findByText(/did not start/i)).toBeInTheDocument();
  });

  it("adopts its own result: an explicit re-suggest clears the local override", async () => {
    // The precedence rule ("your choice always wins") is about *automatic*
    // recomputes — a colleague's ingest must not move anyone's columns. But
    // ticking a single checkbox materializes the current suggestion into a
    // stored override, so without this the button an analyst presses to get a
    // new answer is the one thing guaranteed never to show it: the job runs,
    // the suggestion changes, and the grid keeps the columns it had.
    const { timelinesApi } = await import("@/api/timelines");
    useUiStore.setState({ visibleColumnsByTimeline: { "c1/t1": ["message"] } });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));

    await waitFor(() =>
      expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1", false),
    );
    await waitFor(() =>
      expect(useUiStore.getState().visibleColumnsByTimeline["c1/t1"]).toBeUndefined(),
    );
  });

  it("keeps the override when the run could not be started", async () => {
    // Clearing on failure would drop the analyst's columns for nothing.
    const { timelinesApi } = await import("@/api/timelines");
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValue(new Error("boom"));
    useUiStore.setState({ visibleColumnsByTimeline: { "c1/t1": ["message"] } });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));

    expect(await screen.findByText(/Could not start the suggestion/i)).toBeInTheDocument();
    expect(useUiStore.getState().visibleColumnsByTimeline["c1/t1"]).toEqual(["message"]);
  });

  it("says so in the footer when a local re-suggest fails", async () => {
    // The local path has no dialog to report into. Without this it failed
    // silently: the button re-enabled and nothing else happened, which reads
    // as "the suggestion is identical to what you already have".
    const { timelinesApi } = await import("@/api/timelines");
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValue(new Error("boom"));
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));

    expect(await screen.findByText(/Could not start the suggestion/i)).toBeInTheDocument();
  });

  it("says so in the footer when the opted-in AI shortcut fails", async () => {
    // Opted in already, so no disclosure opens — the footer is the only
    // surface this failure has.
    const { timelinesApi } = await import("@/api/timelines");
    capabilities.agent = true;
    useAuthStore.setState({
      user: testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    });
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValue(new Error("boom"));
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));

    expect(await screen.findByText(/Could not start the suggestion/i)).toBeInTheDocument();
  });

  it("leaves a dialog-owned failure to the dialog", async () => {
    // `recommendMutation` is shared by all three buttons, so the footer has to
    // know when something else is already saying it — in more precise words
    // than the footer has ("your choice was saved", "nothing was sent").
    const { timelinesApi } = await import("@/api/timelines");
    const { authApi } = await import("@/api/auth");
    capabilities.agent = true;
    useAuthStore.setState({ user: testUser() });
    vi.mocked(authApi.updatePreferences).mockResolvedValue(
      testUser({ preferences: { column_advisor_optin: { t1: true } } }),
    );
    vi.mocked(timelinesApi.recommendColumns).mockRejectedValue(new Error("boom"));
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));
    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    expect(await screen.findByText(/did not start/i)).toBeInTheDocument();
    expect(screen.queryByText(/Could not start the suggestion/i)).toBeNull();
  });

  it("asks again on a timeline the analyst has not opted in to", async () => {
    capabilities.agent = true;
    useAuthStore.setState({
      user: testUser({ preferences: { column_advisor_optin: { "some-other-timeline": true } } }),
    });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /suggest with ai/i }));

    expect(await screen.findByText("Suggest columns with AI")).toBeInTheDocument();
  });
});
