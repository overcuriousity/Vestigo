/**
 * ColumnPicker derived-key grouping (PR #54 finding #34): enrichment-derived
 * keys collapse under their parent attribute, search auto-expands them, and
 * selection always uses the full raw key.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ColumnPicker } from "@/components/explorer/ColumnPicker";
import { useUiStore } from "@/stores/ui";
import type { RecommendedColumns } from "@/api/types";

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    recommendColumns: vi.fn().mockResolvedValue({ job_id: "job-1", enabled: true }),
  },
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

beforeEach(() => {
  useUiStore.setState({ visibleColumnsByTimeline: {} });
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
 * evidence, adopting the suggestion clears the local override rather than
 * copying it in, and recomputing is contribute-gated.
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

  it("adopting the suggestion clears the local override", async () => {
    useUiStore.setState({ visibleColumnsByTimeline: { "c1/t1": ["message"] } });
    await renderWithSuggestion({ recommended: SUGGESTION });

    fireEvent.click(screen.getByRole("button", { name: /use suggested/i }));

    // Absent, not equal-to-the-suggestion: a later recomputation must still
    // reach this browser.
    expect(useUiStore.getState().visibleColumnsByTimeline).not.toHaveProperty("c1/t1");
  });

  it("offers no suggestion actions when the timeline has none", async () => {
    await renderWithSuggestion({ recommended: null, canRecommend: true });

    expect(screen.queryByRole("button", { name: /use suggested/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /re-suggest columns/i })).toBeInTheDocument();
  });

  it("hides the re-suggest action without contribute access", async () => {
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: false });

    expect(
      screen.queryByRole("button", { name: /re-suggest columns/i }),
    ).not.toBeInTheDocument();
  });

  it("re-suggesting starts a job and tracks it in the tray", async () => {
    const { timelinesApi } = await import("@/api/timelines");
    const { useJobsStore } = await import("@/stores/jobs");
    useJobsStore.setState({ jobs: {} });
    await renderWithSuggestion({ recommended: SUGGESTION, canRecommend: true });

    fireEvent.click(screen.getByRole("button", { name: /re-suggest columns/i }));

    await waitFor(() => expect(useJobsStore.getState().jobs).toHaveProperty("job-1"));
    expect(vi.mocked(timelinesApi.recommendColumns)).toHaveBeenCalledWith("c1", "t1");
  });
});
