/**
 * Opening a saved chart by id (`?c_chart=<id>`).
 *
 * A saved chart stores the filters it was built under, and three of them —
 * `ids`, `anomalyRunId`, `collapseRoutine` — have no URL representation at
 * all. So a link that reconstructed the chart from `c_*` params could restore
 * the shape but never the scope: an agent chart built over one detector run's
 * 47 events would open as the whole timeline, drawn as if it had always been
 * that shape. Naming the chart instead lets the page read both halves back out
 * of storage, which is the one place they travel together.
 *
 * The rule the feature rests on: `c_chart` means "this is saved chart X", so
 * any edit — to the shape or to the filters — drops it and spells the chart
 * out in full, because after an edit the claim is no longer true.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VisualizePage } from "@/pages/VisualizePage";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";
import type { VizFieldsResponse } from "@/api/types";

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

const fieldsMock = vi.fn();
const fieldTermsMock = vi.fn();
const chartsListMock = vi.fn();
const dispositionsListMock = vi.fn();

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: {
      ...actual.vizApi,
      fields: (...args: unknown[]) => fieldsMock(...args),
      fieldTerms: (...args: unknown[]) => fieldTermsMock(...args),
    },
    savedChartsApi: {
      ...actual.savedChartsApi,
      list: (...args: unknown[]) => chartsListMock(...args),
    },
  };
});

vi.mock("@/api/dispositions", async () => {
  const actual = await vi.importActual<typeof import("@/api/dispositions")>("@/api/dispositions");
  return {
    ...actual,
    dispositionsApi: {
      ...actual.dispositionsApi,
      list: (...args: unknown[]) => dispositionsListMock(...args),
    },
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [
    { token: "artifact", distinct: 12, coverage: 0.98 },
    { token: "hostname", distinct: 30, coverage: 0.9 },
  ],
};

/** A chart scoped the way only an agent proposal can scope one. */
const SCOPED_CHART = {
  id: "chart-1",
  case_id: "c1",
  timeline_id: "t1",
  name: "Run 7 findings",
  config: {
    v: 1,
    chartType: "bar",
    scale: "nominal",
    field: "hostname",
    metric: "count",
    compare: { mode: "off" },
    options: {},
    filters: {
      q: "logon",
      exclusions: { user: ["svc_backup"] },
      eventIds: ["evt-1", "evt-2"],
      runId: "run-7",
    },
  },
  created_at: null,
  updated_at: null,
};

let lastSearch = "";

function LocationProbe() {
  lastSearch = useLocation().search;
  return null;
}

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[entry]}>
          <LocationProbe />
          <Routes>
            <Route
              path="/cases/:caseId/timelines/:timelineId/visualize"
              element={<VisualizePage />}
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  lastSearch = "";
  fieldsMock.mockReset().mockResolvedValue(FIELDS);
  fieldTermsMock
    .mockReset()
    .mockResolvedValue({ field: "hostname", total: 10, distinct: 2, values: [], other_count: 0 });
  chartsListMock.mockReset().mockResolvedValue({ charts: [SCOPED_CHART] });
  dispositionsListMock.mockReset().mockResolvedValue({ dispositions: [] });
});

describe("VisualizePage ?c_chart=", () => {
  it("draws the stored chart, scope included", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    expect(fieldTermsMock.mock.calls[0][2]).toBe("hostname");

    const filters = fieldTermsMock.mock.calls[0][3] as Record<string, unknown>;
    expect(filters.q).toBe("logon");
    expect(filters.exclusions).toEqual({ user: ["svc_backup"] });
    // The three that no reconstructed URL could have carried.
    expect(filters.ids).toEqual(["evt-1", "evt-2"]);
    expect(filters.anomalyRunId).toBe("run-7");
  });

  it("never queries the default chart first", async () => {
    // Same argument as the #147 disposition gate: rendering the params' chart
    // while the saved one loads shows the analyst the wrong chart, then
    // silently swaps it.
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    for (const call of fieldTermsMock.mock.calls) {
      expect(call[2]).toBe("hostname");
      expect((call[3] as Record<string, unknown>).anomalyRunId).toBe("run-7");
    }
  });

  it("survives the field list arriving before the chart", async () => {
    // While the reference resolves, the page's config is the *default* chart —
    // fieldless. The effect that defaults the field would then write a config
    // to the URL, which is a takeover, which drops `c_chart` before the chart
    // it names has loaded: the link would work or not depending on which query
    // won the race.
    let releaseCharts: (v: unknown) => void = () => {};
    chartsListMock.mockReturnValue(
      new Promise((resolve) => {
        releaseCharts = resolve;
      }),
    );

    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");
    // Let the field list *land* and its effects run — releasing the charts any
    // earlier would close the window this test exists to hold open.
    await waitFor(() => expect(screen.getByText(/Chart type/i)).toBeTruthy());
    await act(async () => {
      await Promise.resolve();
    });
    expect(lastSearch).toContain("c_chart=chart-1");
    expect(fieldTermsMock).not.toHaveBeenCalled();

    releaseCharts({ charts: [SCOPED_CHART] });

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    expect(lastSearch).toContain("c_chart=chart-1");
    expect(fieldTermsMock.mock.calls[0][2]).toBe("hostname");
  });

  it("says so when the chart is gone rather than drawing a default", async () => {
    chartsListMock.mockResolvedValue({ charts: [] });
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    expect(await screen.findByText(/no longer exists/i)).toBeTruthy();
  });

  it("says so when the chart cannot be read by this build", async () => {
    chartsListMock.mockResolvedValue({
      charts: [{ ...SCOPED_CHART, config: { ...SCOPED_CHART.config, v: 99 } }],
    });
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    expect(await screen.findByText(/incompatible config version/i)).toBeTruthy();
  });

  it("editing the chart drops the reference and spells the chart out", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: /Ordinal/ }));

    await waitFor(() => expect(lastSearch).not.toContain("c_chart"));
    const params = new URLSearchParams(lastSearch);
    // The edit itself.
    expect(params.get("c_scale")).toBe("ordinal");
    // Everything the reference used to stand for, now written out — the
    // URL-representable filters survive the takeover rather than being
    // dropped along with the reference.
    expect(params.get("c_field")).toBe("hostname");
    expect(params.get("q")).toBe("logon");
    expect(params.get("exclusions")).toContain("svc_backup");
  });
});
