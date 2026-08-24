/**
 * VisualizePage inherited-filters bar.
 *
 * The page's filters come from the URL, the same params the Explorer wrote —
 * but nothing above the chart used to say so, and an unfiltered chart looked
 * identical to a chart of one narrow slice. For a figure that ends up in a
 * report, the scope has to be legible before the chart is read.
 *
 * Removing a chip must also preserve the `c_*` chart config, which lives in the
 * same query string (that is what `filterParamsPreservingChartConfig` is for).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
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
  fields: [{ token: "artifact", distinct: 12, coverage: 0.98 }],
};

const CHART = "c_type=bar&c_scale=nominal&c_field=artifact";

function renderPage(query: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[`/cases/c1/timelines/t1/visualize?${query}`]}>
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

/** The filters object the active chart query was last called with. */
function lastQueryFilters(): Record<string, unknown> {
  return fieldTermsMock.mock.calls.at(-1)![3] as Record<string, unknown>;
}

beforeEach(() => {
  fieldsMock.mockReset().mockResolvedValue(FIELDS);
  fieldTermsMock
    .mockReset()
    .mockResolvedValue({ field: "artifact", total: 10, distinct: 2, values: [], other_count: 0 });
  dispositionsListMock.mockReset().mockResolvedValue({ dispositions: [] });
});

describe("VisualizePage inherited filters", () => {
  it("names every inherited filter as a chip", async () => {
    renderPage(
      `${CHART}&q=powershell&filters=%7B%22user%22%3A%20%5B%22alice%22%5D%7D&exclusions=%7B%22host%22%3A%20%5B%22srv1%22%5D%7D&start=2026-01-01T00:00:00Z&tagsExclude=noise`,
    );

    expect(await screen.findByText("Inherited from Explorer")).toBeTruthy();
    const bar = screen.getByTestId("inherited-filters");
    expect(bar.textContent).toContain("powershell");
    expect(bar.textContent).toContain("alice");
    expect(bar.textContent).toContain("srv1");
    expect(bar.textContent).toContain("noise");
    expect(bar.textContent).toContain("2026-01-01");
  });

  it("says so explicitly when nothing is filtered", async () => {
    renderPage(CHART);

    expect(await screen.findByText(/No filters — charting the whole timeline/)).toBeTruthy();
  });

  it("removing a chip narrows the chart but keeps the chart config", async () => {
    renderPage(`${CHART}&q=powershell&filters=%7B%22user%22%3A%20%5B%22alice%22%5D%7D`);

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    expect(lastQueryFilters().q).toBe("powershell");

    const searchChip = [...screen.getByTestId("inherited-filters").querySelectorAll("span")].find(
      (el) => el.textContent === "search=powershell",
    )!;
    fireEvent.click(searchChip.querySelector("button")!);

    await waitFor(() => expect(lastQueryFilters().q).toBeUndefined());
    // The field filter survives, and so does the chart itself — a chip removal
    // that dropped `c_field` would strand the page on the field picker.
    expect(lastQueryFilters().filters).toEqual({ user: ["alice"] });
    expect(screen.getByTestId("inherited-filters").textContent).toContain("alice");
    expect(fieldTermsMock.mock.calls.at(-1)![2]).toBe("artifact");
  });

  it("clears every filter at once", async () => {
    renderPage(`${CHART}&q=powershell&filters=%7B%22user%22%3A%20%5B%22alice%22%5D%7D`);

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Clear all" }));

    expect(await screen.findByText(/No filters — charting the whole timeline/)).toBeTruthy();
    await waitFor(() => expect(lastQueryFilters().q).toBeUndefined());
    expect(lastQueryFilters().filters).toBeUndefined();
  });

  it("resets both time bounds in one click after a brush-zoom", async () => {
    renderPage(`${CHART}&start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z`);

    await waitFor(() => expect(fieldTermsMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: /Reset range/ }));

    await waitFor(() => expect(lastQueryFilters().start).toBeUndefined());
    expect(lastQueryFilters().end).toBeUndefined();
  });
});
