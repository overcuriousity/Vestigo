/**
 * The field-optional figures (Cumulative, Calendar) against the two effects
 * that used to overrule the analyst on them (#332):
 *
 * 1. The default-field effect had no `fieldOptional` guard, so an explicit
 *    "No field — count every event" was reverted to the highest-coverage
 *    field on the next render. Not cosmetic: with a field set, `/viz/calendar`
 *    counts only events whose field is non-empty and `resolveChartOptions`
 *    flips `quantity` back to sum/distinct.
 * 2. The `time:`-field scale suggestion re-picked `chartType`, unlike its
 *    numeric sibling, which documents the rule both should follow — these
 *    figures were chosen *before* the field, so a suggestion may move the
 *    treat-as but never the figure.
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
const cumulativeMock = vi.fn();
const calendarMock = vi.fn();
const savedChartsMock = vi.fn();
const dispositionsMock = vi.fn();

vi.mock("@/api/dispositions", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/dispositions")>("@/api/dispositions");
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
      cumulative: (...args: unknown[]) => cumulativeMock(...args),
      calendar: (...args: unknown[]) => calendarMock(...args),
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
    {
      token: "time:hour_of_day",
      distinct: 24,
      coverage: null,
      label: "Hour of day (UTC)",
    },
  ],
};

/**
 * Block until the field list has actually reached the rail. Opening the combo
 * renders one option per field, which cannot happen before `fieldsQuery.data`
 * resolves — the same data the default-field effect keys off. Waiting on the
 * mock alone is not enough: it settles a render earlier than the effect runs,
 * so the assertions raced it and passed against the bug.
 */
async function settleFieldList() {
  const combo = await screen.findByRole("combobox", { name: /^Field/ });
  fireEvent.focus(combo);
  await screen.findByRole("listbox");
  await screen.findByRole("option", { name: /artifact/ });
  fireEvent.keyDown(combo, { key: "Escape" });
  fireEvent.blur(combo);
}

let lastSearch = "";
function LocationSpy() {
  lastSearch = useLocation().search;
  return null;
}

function renderPage(entry: string) {
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
  cumulativeMock.mockResolvedValue({
    kind: "cumulative",
    field: null,
    quantity: "events",
    interval_seconds: 3600,
    min: "2026-07-20T00:00:00+00:00",
    max: "2026-07-20T06:00:00+00:00",
    buckets: [{ start: "2026-07-20T00:00:00+00:00", value: 1, cumulative: 1 }],
    total: 1,
    events: 1,
    unparsed: 0,
  });
  calendarMock.mockResolvedValue({
    kind: "calendar",
    field: null,
    timezone: "UTC",
    start: "2026-07-13",
    end: "2026-07-19",
    days: [{ date: "2026-07-15", count: 2 }],
    total: 2,
    max_count: 2,
    weeks: 1,
    weeks_total: 1,
    truncated: false,
    dropped: 0,
  });
  savedChartsMock.mockResolvedValue({ charts: [] });
  dispositionsMock.mockResolvedValue({ dispositions: [] });
});

describe("field-optional figures", () => {
  it("leaves a fieldless cumulative fieldless instead of defaulting a field", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_type=cumulative&c_scale=nominal");
    // The default-field effect fires when `fieldsQuery.data` lands, so the
    // assertion has to wait for something that can only be true *after* that.
    await settleFieldList();
    await waitFor(() =>
      expect(
        (screen.getByRole("combobox", { name: /^Field/ }) as HTMLInputElement).value,
        // `ChartRail`'s NO_FIELD sentinel: the rail's own record that no
        // field is selected, which is exactly what the effect used to overwrite.
      ).toBe("__viz_no_field__"),
    );
    // The query runs — a fieldless cumulative is a complete spec — and every
    // call counts every event rather than filtering on a field nobody chose.
    await waitFor(() => expect(cumulativeMock).toHaveBeenCalled());
    for (const call of cumulativeMock.mock.calls) {
      expect(call[3]?.field ?? null).toBeNull();
      expect(call[3]?.quantity).toBe("events");
    }
    expect(lastSearch).not.toContain("c_field");
  });

  it("leaves a fieldless calendar fieldless too", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_type=calendar&c_scale=nominal");
    await settleFieldList();
    await waitFor(() =>
      expect(
        (screen.getByRole("combobox", { name: /^Field/ }) as HTMLInputElement).value,
        // `ChartRail`'s NO_FIELD sentinel: the rail's own record that no
        // field is selected, which is exactly what the effect used to overwrite.
      ).toBe("__viz_no_field__"),
    );
    await waitFor(() => expect(calendarMock).toHaveBeenCalled());
    for (const call of calendarMock.mock.calls) {
      expect(call[3]?.field ?? null).toBeNull();
    }
    expect(lastSearch).not.toContain("c_field");
  });

  it("keeps the figure when a time field is picked on a cumulative", async () => {
    renderPage("/cases/c1/timelines/t1/visualize?c_type=cumulative&c_scale=nominal");
    await waitFor(() => expect(cumulativeMock).toHaveBeenCalled());
    fireEvent.focus(await screen.findByRole("combobox", { name: /^Field/ }));
    await screen.findByRole("listbox");
    fireEvent.mouseDown(
      await screen.findByRole("option", { name: /Hour of day \(UTC\)/ }),
    );
    // The treat-as may move; the figure may not. It used to become a bar.
    await waitFor(() => expect(lastSearch).toContain("c_field=time%3Ahour_of_day"));
    expect(lastSearch).toContain("c_type=cumulative");
    expect(lastSearch).not.toContain("c_type=bar");
  });
});
