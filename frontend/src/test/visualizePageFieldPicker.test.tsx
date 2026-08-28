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
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [
    { token: "artifact", distinct: 12, coverage: 0.98 },
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

function renderPage() {
  lastSearch = "";
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[START]}>
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
});

/** Open the primary field combo — `FieldCombo` opens its list on focus. */
const openFieldPicker = async () => {
  const combo = (await screen.findAllByRole("combobox"))[0];
  fireEvent.focus(combo);
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
      expect((screen.getAllByRole("combobox")[0] as HTMLInputElement).value).toBe(
        "time:hour_of_day",
      ),
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
