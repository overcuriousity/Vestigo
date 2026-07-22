/**
 * ChartProposalCard (A9) smoke test: fetches live data through the mocked
 * vizApi/eventsApi per `AgentChartSpec.kind`, renders the matching chart
 * component, and the Save button posts through savedChartsApi.create.
 *
 * jsdom has no ResizeObserver — same polyfill as vizCharts.smoke.test.tsx.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChartProposalCard } from "@/components/agent/ChartProposalCard";
import type { AgentChartSpec } from "@/api/agent";
import type { FieldTermsResponse, FieldNumericResponse, HistogramResponse } from "@/api/types";

beforeAll(() => {
  class FakeResizeObserver {
    private cb: ResizeObserverCallback;
    constructor(cb: ResizeObserverCallback) {
      this.cb = cb;
    }
    observe(target: Element) {
      this.cb(
        [{ target, contentRect: { width: 400 } } as unknown as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    }
    unobserve() {}
    disconnect() {}
  }
  // @ts-expect-error -- jsdom has no native ResizeObserver
  global.ResizeObserver = FakeResizeObserver;
});

const fieldTermsMock = vi.fn();
const fieldNumericMock = vi.fn();
const punchcardMock = vi.fn();
const savedChartsCreateMock = vi.fn();
const histogramMock = vi.fn();
const fieldTimeseriesMock = vi.fn();
const fieldPivotMock = vi.fn();

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: {
      fieldTerms: (...args: unknown[]) => fieldTermsMock(...args),
      fieldNumeric: (...args: unknown[]) => fieldNumericMock(...args),
      punchcard: (...args: unknown[]) => punchcardMock(...args),
      fieldTimeseries: (...args: unknown[]) => fieldTimeseriesMock(...args),
      fieldPivot: (...args: unknown[]) => fieldPivotMock(...args),
      fieldScatter: vi.fn(),
      fieldCorrelation: vi.fn(),
      fieldNumericGrouped: vi.fn(),
      compare: vi.fn(),
    },
    savedChartsApi: {
      create: (...args: unknown[]) => savedChartsCreateMock(...args),
    },
  };
});
vi.mock("@/api/events", () => ({
  eventsApi: { histogram: (...args: unknown[]) => histogramMock(...args) },
}));

const CASE = "c1";
const TL = "t1";

const TERMS: FieldTermsResponse = {
  field: "artifact",
  total: 100,
  distinct: 3,
  other_count: 10,
  values: [
    { value: "GET", count: 60 },
    { value: "POST", count: 30 },
  ],
};

const NUMERIC: FieldNumericResponse = {
  field: "attr:bytes",
  count: 100,
  min: 0,
  max: 100,
  mean: 50,
  stddev: 20,
  skewness: 0,
  points: null,
  bin_rule: "manual",
  bin_width: 50,
  quantiles: {},
  bins: [{ x0: 0, x1: 50, count: 60 }],
};

const HISTOGRAM: HistogramResponse = {
  interval_seconds: 3600,
  min: "2024-01-01T00:00:00Z",
  max: "2024-01-01T02:00:00Z",
  buckets: [{ start: "2024-01-01T00:00:00Z", count: 5 }],
};

function renderCard(spec: AgentChartSpec) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <ChartProposalCard
          caseId={CASE}
          timelineId={TL}
          title="Artifact spread"
          description="top artifacts"
          spec={spec}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  fieldTermsMock.mockResolvedValue(TERMS);
  fieldNumericMock.mockResolvedValue(NUMERIC);
  punchcardMock.mockResolvedValue({ kind: "punchcard", total: 10, max_count: 5, cells: [] });
  histogramMock.mockResolvedValue(HISTOGRAM);
  savedChartsCreateMock.mockResolvedValue({ chart: { id: "sc1" } });
  fieldTimeseriesMock.mockResolvedValue({
    field: "attr:status",
    series: [{ value: "a", counts: [1, 2] }],
    interval_seconds: 3600,
    min: "2026-01-01T00:00:00Z",
    max: "2026-01-02T00:00:00Z",
  });
  fieldPivotMock.mockResolvedValue({
    kind: "pivot",
    field_x: "attr:user",
    field_y: "time:hour_of_day",
    x_values: ["root"],
    y_values: ["00", "01"],
    x_distinct: 1,
    y_distinct: 24,
    cells: [{ x: "root", y: "00", count: 3 }],
    total: 3,
  });
});

describe("ChartProposalCard", () => {
  it("renders a bar chart for kind=terms", async () => {
    const { container } = renderCard({ kind: "terms", field: "artifact" });
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
    expect(fieldTermsMock.mock.calls[0][2]).toBe("artifact");
  });

  it("renders a histogram for kind=numeric", async () => {
    const { container } = renderCard({ kind: "numeric", field: "attr:bytes" });
    await waitFor(() => expect(fieldNumericMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("renders a time histogram for kind=compare_time without comparison_filters", async () => {
    const { container } = renderCard({ kind: "compare_time" });
    await waitFor(() => expect(histogramMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("renders a punchcard for kind=punchcard", async () => {
    const { container } = renderCard({ kind: "punchcard" });
    await waitFor(() => expect(punchcardMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("shows an error message when the fetch fails", async () => {
    fieldTermsMock.mockRejectedValue(new Error("boom"));
    renderCard({ kind: "terms", field: "artifact" });
    await screen.findByText(/Couldn't load this chart/);
  });

  it("Save posts through savedChartsApi.create with the mapped ChartConfig", async () => {
    renderCard({ kind: "terms", field: "artifact", limit: 20 });
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    const input = screen.getByPlaceholderText("Save as…");
    fireEvent.change(input, { target: { value: "my chart" } });
    fireEvent.click(screen.getByLabelText("Save chart"));
    await waitFor(() => expect(savedChartsCreateMock).toHaveBeenCalled());
    const [caseId, timelineId, name, config] = savedChartsCreateMock.mock.calls[0];
    expect(caseId).toBe(CASE);
    expect(timelineId).toBe(TL);
    expect(name).toBe("my chart");
    expect(config).toMatchObject({ v: 1, chartType: "bar", field: "artifact" });
  });

  it("Open in Visualize link carries the mapped chart-config params", async () => {
    renderCard({ kind: "numeric", field: "attr:bytes" });
    await waitFor(() => expect(fieldNumericMock).toHaveBeenCalled());
    const link = screen.getByRole("link", { name: /Open in Visualize/ });
    const href = link.getAttribute("href")!;
    expect(href).toContain(`/cases/${CASE}/timelines/${TL}/visualize`);
    expect(href).toContain("c_type=histogram");
    expect(href).toContain("c_field=attr%3Abytes");
  });
});

/**
 * The bug this contract replaced: `propose_chart` could not express "pie", and
 * even a correct chartType would still have drawn a bar, because the render
 * switch keyed on the aggregation that fed it rather than the mark.
 */
describe("ChartProposalCard renders the requested mark, not the aggregation's default", () => {
  it("draws a pie — not a bar — for chart_type=pie", async () => {
    renderCard({ chart_type: "pie", field: "artifact" });
    // Scoped to the chart box: the card header carries a lucide icon that is
    // itself an <svg><path>. PieChart emits arc <path>s; BarChart never does.
    const canvas = screen.getByTestId("agent-chart-canvas");
    await waitFor(() => expect(canvas.querySelector(".animate-spin")).toBeNull());
    expect(canvas.querySelector("svg path")).not.toBeNull();
  });

  it("draws no pie arcs for chart_type=bar over the same aggregation", async () => {
    // The discriminator is the arc: BarChart lays bars out against a measured
    // width, which is 0 under jsdom, so asserting on <rect> would be checking
    // the harness rather than the mark.
    renderCard({ chart_type: "bar", field: "artifact" });
    const canvas = screen.getByTestId("agent-chart-canvas");
    // Wait for the loading spinner to clear — it is itself an <svg><path>.
    await waitFor(() => expect(canvas.querySelector(".animate-spin")).toBeNull());
    expect(canvas.querySelector("svg")).not.toBeNull();
    expect(canvas.querySelector("svg path")).toBeNull();
  });

  it("draws a sankey for chart_type=sankey, sharing pivot's aggregation", async () => {
    const { container } = renderCard({
      chart_type: "sankey",
      field: "attr:user",
      field_y: "time:hour_of_day",
    });
    await waitFor(() => expect(fieldPivotMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("draws a pivot heatmap for a country x hour-of-day proposal", async () => {
    const { container } = renderCard({
      chart_type: "pivot",
      field: "attr:user",
      field_y: "time:hour_of_day",
    });
    await waitFor(() => expect(fieldPivotMock).toHaveBeenCalled());
    expect(container.querySelector("svg")).not.toBeNull();
    expect(fieldPivotMock.mock.calls[0][3]).toBe("time:hour_of_day");
  });

  it("passes the spec's bucket count through instead of a hardcoded 60", async () => {
    renderCard({
      chart_type: "line",
      field: "attr:status",
      scale: "ratio",
      options: { buckets: 20, top_n: 5 },
    });
    await waitFor(() => expect(fieldTimeseriesMock).toHaveBeenCalled());
    const call = fieldTimeseriesMock.mock.calls[0];
    expect(call[4]).toBe(20);
    expect(call[5]).toBe(5);
  });
});

describe("facetted proposals", () => {
  it("draws one panel per facet value instead of the unfacetted chart", async () => {
    // The panel list comes from a terms query on the facet field; each panel
    // then re-runs the mark's own endpoint with the value applied as a filter.
    fieldTermsMock.mockImplementation((_c, _t, field) =>
      Promise.resolve(
        field === "attr:user"
          ? {
              field: "attr:user",
              total: 100,
              distinct: 5,
              other_count: 12,
              values: [
                { value: "alice", count: 60 },
                { value: "bob", count: 28 },
              ],
            }
          : TERMS,
      ),
    );
    renderCard({
      chart_type: "bar",
      field: "artifact",
      facet: { field: "attr:user", limit: 2 },
    } as AgentChartSpec);

    await waitFor(() => expect(screen.getByText(/attr:user = alice/)).toBeTruthy());
    expect(screen.getByText(/attr:user = bob/)).toBeTruthy();
    // The omission is stated, not merged into an "Other" panel.
    expect(screen.getByText(/3 further values/)).toBeTruthy();
    // Each panel's own query carried the facet value as a filter.
    const panelCalls = fieldTermsMock.mock.calls.filter((c) => c[2] === "artifact");
    expect(panelCalls.length).toBe(2);
    expect(
      panelCalls.every((c) => JSON.stringify(c[3]).includes("alice") || JSON.stringify(c[3]).includes("bob")),
    ).toBe(true);
  });
});
