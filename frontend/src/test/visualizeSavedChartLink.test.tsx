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
import type { Disposition, VizFieldsResponse } from "@/api/types";

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

const fieldsMock = vi.fn();
const fieldTermsMock = vi.fn();
// Mocked deliberately, even though a `c_chart` chart must never trigger it: the
// scale probe is the one API call on this page that *writes back* to the URL,
// so leaving it to reject (as an unmocked call does in jsdom) is exactly how a
// takeover of the reference stays invisible to this file.
const fieldNumericMock = vi.fn();
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
      fieldNumeric: (...args: unknown[]) => fieldNumericMock(...args),
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

/**
 * A chart stored with routine collapse on, and *nothing else* the URL cannot
 * carry — so anything this page says about dropped scope for it is wrong by
 * construction.
 */
const COLLAPSED_CHART = {
  ...SCOPED_CHART,
  id: "chart-2",
  name: "Logons, routine collapsed",
  config: {
    ...SCOPED_CHART.config,
    filters: { q: "logon", collapseRoutine: true },
  },
};

function routineDisposition(id: string): Disposition {
  return {
    id,
    case_id: "c1",
    timeline_id: "t1",
    kind: "routine",
    detector: "log_template",
    field: "template_id",
    value: "4736",
    source_id: null,
    event_id: null,
    note: null,
    details: null,
    created_by: null,
    created_at: null,
  };
}

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
  fieldNumericMock
    .mockReset()
    .mockResolvedValue({ field: "hostname", count: 0, bins: [], points: [], min: null, max: null });
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
    await waitFor(() => expect(screen.getByText(/^Treat as$/)).toBeTruthy());
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

  it("keeps naming the chart once the page has settled", async () => {
    // The page's own defaulting effects — field, scale, chart type, metric —
    // exist for a chart the analyst is building. A stored chart already
    // answered all four, and any one of them writing back would rewrite the
    // URL as `c_*` params: `c_chart` gone, and with it the three filter
    // members params cannot carry. Nothing may write the URL while it names a
    // chart; only the analyst's own edit may.
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(lastSearch).toBe("?c_chart=chart-1");
    // The scale probe is what would have overwritten the stored nominal/bar
    // pair with its own guess; a named chart must not even ask.
    expect(fieldNumericMock).not.toHaveBeenCalled();
  });

  it("still defaults the chart when the reference is broken", async () => {
    // The guard is on the *reference*, not on the presence of `c_chart`: a
    // link to a deleted chart falls through to the params, where the page is
    // building a chart again and the defaults are wanted.
    chartsListMock.mockResolvedValue({ charts: [] });
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");

    await waitFor(() => expect(new URLSearchParams(lastSearch).get("c_field")).toBe("artifact"));
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

  it("falls through to a default chart when the chart list cannot be fetched", async () => {
    // A fetch that *failed* is not a chart that is still arriving. Waiting on
    // one suspends the page with nothing drawn and nothing said, so the
    // reference settles as broken and the params take over — the same
    // degradation a deleted chart already got.
    chartsListMock.mockRejectedValue(new Error("network"));
    // `c_type=bar` only so the fallback chart is one whose data arrives
    // through a mocked call — the point being asserted is that data arrives at
    // all, which is what an unsettled reference prevented.
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1&c_type=bar");

    await waitFor(() => expect(new URLSearchParams(lastSearch).get("c_field")).toBe("artifact"));
    expect(await screen.findByText(/could not be loaded/i)).toBeTruthy();
    // The scope becomes ready rather than waiting forever on a fetch that
    // already failed, so the fallback chart actually draws.
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
  });

  it("carries unrelated query params through a takeover", async () => {
    // This page owns `c_*` and the filter params. Rebuilding the URL from
    // scratch would drop everything else in it, which nothing writes today —
    // which is exactly why the loss would go unnoticed.
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1&tour=viz");
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: "Ordered categories" }));

    await waitFor(() => expect(lastSearch).not.toContain("c_chart"));
    expect(new URLSearchParams(lastSearch).get("tour")).toBe("viz");
  });

  it("editing the chart drops the reference and spells the chart out", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-1");
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: "Ordered categories" }));

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

  it("lets the reveal toggle uncollapse a chart stored with routine collapse on", async () => {
    // Routine collapse is the one stored member this page must *not* honour
    // from storage: it is derived from the live disposition set (#147), and
    // the resolved `filters` only ever adds the flag. A stored `true` reaching
    // that spread would therefore survive the reveal — the toggle would redraw
    // nothing and the analyst would have no way to see the muted events.
    chartsListMock.mockResolvedValue({ charts: [COLLAPSED_CHART] });
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });

    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-2");

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    expect((fieldTermsMock.mock.calls[0][3] as Record<string, unknown>).collapseRoutine).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /Show routine events/i }));

    await waitFor(() => {
      const last = fieldTermsMock.mock.calls.at(-1)![3] as Record<string, unknown>;
      expect(last.collapseRoutine).toBeUndefined();
    });
    // Revealing is not an edit: the URL still names the chart.
    expect(lastSearch).toContain("c_chart=chart-2");
  });

  it("does not claim an edit dropped routine collapse, because it did not", async () => {
    // The take-over warning exists so a chart that genuinely narrows cannot
    // widen in silence. Collapse is re-derived from dispositions on every
    // render, so it survives the take-over — reporting it would train the
    // analyst to ignore the one banner that matters.
    chartsListMock.mockResolvedValue({ charts: [COLLAPSED_CHART] });
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });

    renderPage("/cases/c1/timelines/t1/visualize?c_chart=chart-2");
    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: "Ordered categories" }));

    await waitFor(() => expect(lastSearch).not.toContain("c_chart"));
    expect(screen.queryByText(/dropped/i)).toBeNull();
    // And the collapse itself is still applied, from the live dispositions.
    const last = fieldTermsMock.mock.calls.at(-1)![3] as Record<string, unknown>;
    expect(last.collapseRoutine).toBe(true);
  });
});
