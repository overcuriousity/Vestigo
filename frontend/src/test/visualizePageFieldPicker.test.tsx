/**
 * VisualizePage field picker + the virtual-time-field auto-probe bypass.
 *
 * Two things are pinned here that no render assertion elsewhere can see:
 *
 * 1. Picking a `time:` field issues **no** `field_numeric_stats` call. The
 *    probe would scan the timeline only to report `count: 0` (time parts are
 *    zero-padded strings), then land the analyst on nominal/bar — wrong for a
 *    field whose scale is known statically.
 * 2. The scale it lands on comes from TIME_FIELDS, and the chart type from
 *    `defaultChartTypeForScale` — never the naive `chartTypesFor(s)[0]`,
 *    which is the field-free `time` histogram for every scale.
 *
 * The `auto-change notices` block below pins the other half of #298: every
 * control the rail moves on the analyst's behalf says which one moved and
 * why, says it only when something actually moved, and never says it about a
 * chart the rail did not build.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
const fieldNumericMock = vi.fn();
const fieldTermsMock = vi.fn();
const savedChartsMock = vi.fn();
const dispositionsMock = vi.fn();

// Every chart query waits on the disposition set (`scopeReady`), so the
// numeric probe never fires without it — which is what these tests read.
vi.mock("@/api/dispositions", async () => {
  const actual = await vi.importActual<typeof import("@/api/dispositions")>(
    "@/api/dispositions",
  );
  return {
    ...actual,
    dispositionsApi: {
      ...actual.dispositionsApi,
      list: (...args: unknown[]) => dispositionsMock(...args),
    },
  };
});

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: {
      ...actual.vizApi,
      fields: (...args: unknown[]) => fieldsMock(...args),
      fieldNumeric: (...args: unknown[]) => fieldNumericMock(...args),
      fieldTerms: (...args: unknown[]) => fieldTermsMock(...args),
    },
    savedChartsApi: {
      ...actual.savedChartsApi,
      list: (...args: unknown[]) => savedChartsMock(...args),
    },
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [
    { token: "artifact", distinct: 12, coverage: 0.98 },
    { token: "data_type", distinct: 8, coverage: 0.9 },
    // Virtual entries carry null stats and a label — the shape viz.py emits.
    {
      token: "time:hour_of_day",
      distinct: 24,
      coverage: null,
      label: "Hour of day (UTC)",
    },
    { token: "time:date", distinct: null, coverage: null, label: "Date (UTC)" },
  ],
};

// Start on a chart type that needs a field, so the picker is rendered at all
// — the default `time` histogram shows "— event count —" instead.
const START = "/cases/c1/timelines/t1/visualize?c_type=bar&c_scale=nominal&c_field=artifact";

/** MemoryRouter never touches window.location — capture its search string. */
let lastSearch = "";
function LocationSpy() {
  lastSearch = useLocation().search;
  return null;
}

function renderPage(entry: string = START) {
  lastSearch = "";
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[entry]}>
          <LocationSpy />
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
  vi.clearAllMocks();
  fieldsMock.mockResolvedValue(FIELDS);
  fieldNumericMock.mockResolvedValue({
    field: "artifact",
    count: 0,
    min: null,
    max: null,
    mean: null,
    stddev: null,
    quantiles: {},
    bins: [],
  });
  fieldTermsMock.mockResolvedValue({
    field: "artifact",
    total: 10,
    distinct: 2,
    other_count: 0,
    values: [{ value: "FILE", count: 10 }],
  });
  savedChartsMock.mockResolvedValue({ charts: [] });
  dispositionsMock.mockResolvedValue({ dispositions: [] });
});

/** Open the primary field combo — `FieldCombo` opens its list on focus.
 * By name, not by index: the rail reads scale → chart type → field now (#298),
 * so index 0 is the chart-type Select. */
const openFieldPicker = async () => {
  fireEvent.focus(await screen.findByRole("combobox", { name: /^Field/ }));
  await screen.findByRole("listbox");
};

/** Commit a row — the list commits on mousedown, so focus never leaves the
 * input and the blur handler cannot drop the pick first. */
const pickField = async (label: string) =>
  fireEvent.mouseDown(await screen.findByRole("option", { name: new RegExp(label) }));

describe("VisualizePage field picker", () => {
  it("names a virtual field by its label and marks it as a time field", async () => {
    renderPage();
    await openFieldPicker();
    await waitFor(() => {
      expect(screen.getAllByText("Hour of day (UTC)").length).toBeGreaterThan(0);
    });
    // The raw token is never shown for a virtual field.
    expect(screen.queryByText("time:hour_of_day")).toBeNull();
    expect(screen.getAllByText("(time field)").length).toBeGreaterThan(0);
  });

  it("renders no distinct count for a field whose count is null", async () => {
    renderPage();
    await openFieldPicker();
    await waitFor(() => expect(screen.getAllByText("Date (UTC)").length).toBeGreaterThan(0));
    // Never "(null distinct)" — the virtual branch wins before the guard.
    expect(screen.queryByText(/null distinct/)).toBeNull();
  });

  it("shows the measured distinct count for an ordinary field", async () => {
    renderPage();
    await openFieldPicker();
    await waitFor(() => {
      expect(screen.getAllByText("(12 distinct)").length).toBeGreaterThan(0);
    });
  });
});

describe("VisualizePage time-field auto-probe bypass", () => {
  it("never probes numeric-ness for a virtual time field", async () => {
    renderPage();
    await openFieldPicker();
    await pickField("Hour of day \\(UTC\\)");

    // The box holds the raw token once committed — the friendly label lives on
    // the list row, and the token is what `c_field` carries.
    await waitFor(() =>
      expect(
        (screen.getByRole("combobox", { name: /^Field/ }) as HTMLInputElement).value,
      ).toBe("time:hour_of_day"),
    );
    // The assertion that matters: no field_numeric_stats scan was issued for
    // the time field. Any earlier call was for the default `artifact` pick.
    const timeFieldProbes = fieldNumericMock.mock.calls.filter((c) =>
      String(c[2]).startsWith("time:"),
    );
    expect(timeFieldProbes).toEqual([]);
  });

  it("takes an ordinal time field's scale statically and charts it as a bar", async () => {
    renderPage();
    await openFieldPicker();
    await pickField("Hour of day \\(UTC\\)");
    await waitFor(() => {
      expect(new URLSearchParams(lastSearch).get("c_field")).toBe("time:hour_of_day");
    });
    await waitFor(() => {
      // Scale comes from TIME_FIELDS, not from a probe.
      expect(new URLSearchParams(lastSearch).get("c_scale")).toBe("ordinal");
    });
    // ...and the chart type from defaultChartTypeForScale. The naive
    // chartTypesFor("ordinal")[0] would be "time" — a field-free chart.
    expect(new URLSearchParams(lastSearch).get("c_type")).toBe("bar");
  });
});

/**
 * What the rail changed on the analyst's behalf, and whether it says the true
 * reason (#298). Both cases below used to render a claim about the wrong
 * thing entirely, which is worse than the silence the notice replaced.
 */
describe("VisualizePage auto-change notices", () => {
  it("clears a second field the primary just took over, and names what moved", async () => {
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=pivot&c_scale=nominal&c_field=artifact&c_field_y=data_type",
    );

    fireEvent.focus(await screen.findByRole("combobox", { name: "Field (X)" }));
    fireEvent.mouseDown(await screen.findByRole("option", { name: /data_type/ }));

    // X and Y must differ and the Y list drops whatever X holds, so leaving
    // `data_type` in Y left it unreachable — and disclosed as "not in this
    // timeline's reported fields", a claim about a field that plainly is.
    await waitFor(() => expect(lastSearch).not.toContain("c_field_y"));
    expect(await screen.findByText(/Field \(Y\) cleared/)).toBeInTheDocument();
    expect(screen.queryByText(/not in this timeline/i)).toBeNull();
  });

  it("names the scale in the article the scale actually takes", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("radio", { name: /Interval/ }));

    // Bar is nominal/ordinal only, so the switch is a genuine scale clamp —
    // and "on a interval scale" is not English.
    expect(await screen.findByText(/on an interval scale/)).toBeInTheDocument();
  });

  it("refuses a second field the primary already holds, and says why there", async () => {
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=pivot&c_scale=nominal&c_field=artifact&c_field_y=data_type",
    );

    // The Y list drops whatever X holds, but the box takes free text — so the
    // token can still be typed in, and committing it used to render the
    // combo's unknown-field disclosure. That is a claim about the wrong
    // problem: `artifact` is in this timeline's inventory, it is simply the X
    // field already.
    const y = await screen.findByRole("combobox", { name: "Field (Y)" });
    fireEvent.focus(y);
    fireEvent.change(y, { target: { value: "artifact" } });
    fireEvent.keyDown(y, { key: "Enter" });

    expect(await screen.findByText(/artifact is already the X field/)).toBeInTheDocument();
    expect(screen.queryByText(/not in this timeline/i)).toBeNull();
    // Y is left exactly as it was: X is the axis the chart is built on, so
    // there is no mirror of the X→Y takeover to make here.
    expect(new URLSearchParams(lastSearch).get("c_field_y") ?? "data_type").toBe("data_type");
  });

  it("names the re-pick when a field turns out to have no numeric values", async () => {
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=box&c_scale=ratio&c_field=artifact&c_field_y=data_type",
    );

    // Box takes an *optional* second field, so its primary picker is labelled
    // "Field", not "Field (X)".
    fireEvent.focus(await screen.findByRole("combobox", { name: /^Field/ }));
    fireEvent.mouseDown(await screen.findByRole("option", { name: /data_type/ }));

    // Two controls the analyst never touched move here — the probe comes back
    // empty, so a ratio Box plot becomes a nominal Bar. It used to move both
    // in silence *and* null the notice, wiping the "Group by cleared" line the
    // analyst's own edit had put there a moment earlier.
    expect(
      await screen.findByText(
        /data_type has no numeric values — scale set to nominal, chart set to Bar/,
      ),
    ).toBeInTheDocument();
    await waitFor(() => expect(new URLSearchParams(lastSearch).get("c_type")).toBe("bar"));
    expect(new URLSearchParams(lastSearch).get("c_scale")).toBe("nominal");
  });

  it("says nothing when the probe lands on the chart already on screen", async () => {
    renderPage();

    fireEvent.focus(await screen.findByRole("combobox", { name: /^Field/ }));
    fireEvent.mouseDown(await screen.findByRole("option", { name: /data_type/ }));

    await waitFor(() =>
      expect(new URLSearchParams(lastSearch).get("c_field")).toBe("data_type"),
    );
    // The chart is already nominal/bar, which is where the non-numeric answer
    // points — nothing moved, so there is nothing to announce, and announcing
    // it would be a false statement about the chart on screen. (The same early
    // return is what leaves a standing notice from the analyst's own edit
    // alone.)
    expect(screen.queryByText(/scale set to/)).toBeNull();
    expect(new URLSearchParams(lastSearch).get("c_scale")).toBe("nominal");
    expect(new URLSearchParams(lastSearch).get("c_type")).toBe("bar");
  });

  it("drops the notice when a saved chart takes the canvas over", async () => {
    savedChartsMock.mockResolvedValue({
      charts: [
        {
          id: "ch1",
          name: "Saved bar",
          config: { v: 1, chartType: "bar", scale: "nominal", field: "data_type" },
        },
      ],
    });
    renderPage();

    fireEvent.click(await screen.findByRole("radio", { name: /Interval/ }));
    expect(await screen.findByText(/on an interval scale/)).toBeInTheDocument();

    // The page does not remount for a saved chart — `c_chart` is a param on
    // the same route — so the notice used to survive onto a stored chart the
    // rail re-picked nothing for, claiming a move that never happened to it.
    fireEvent.click(await screen.findByRole("button", { name: "Saved bar" }));

    await waitFor(() => expect(screen.queryByText(/on an interval scale/)).toBeNull());
  });
});
