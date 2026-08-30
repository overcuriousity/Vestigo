/**
 * A fieldless Cumulative or Calendar is a complete spec: both declare
 * `field: "optional"` and both render "count every event" without one. The
 * hardcoded time/punchcard pair in `specComplete` did not grow when they
 * arrived, so an agent card or a Story block showed the incomplete-spec
 * message instead of the chart the Visualize page drew fine (#332).
 */
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { ChartCanvas } from "@/components/viz/ChartCanvas";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { ChartType } from "@/components/viz/lib/chartConfig";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

installFakeResizeObserver();

const MESSAGE = "This chart is missing a field, so there is nothing to plot.";

function renderCanvas(config: ChartConfig) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ChartCanvas caseId="c1" timelineId="t1" config={config} />
    </QueryClientProvider>,
  );
}

describe("ChartCanvas — spec completeness", () => {
  it("does not call a fieldless field-optional figure incomplete", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    const optional = (Object.keys(CHART_META) as ChartType[]).filter(
      (c) => CHART_META[c].inputs.field === "optional",
    );
    // Guards the premise: if the set ever empties, this test proves nothing.
    expect(optional).toEqual(expect.arrayContaining(["cumulative", "calendar"]));
    for (const chartType of optional) {
      const { unmount } = renderCanvas({
        ...DEFAULT_CHART_CONFIG,
        chartType,
        field: null,
      });
      expect(screen.queryByText(MESSAGE)).toBeNull();
      unmount();
    }
  });

  it("still calls a fieldless field-required figure incomplete", () => {
    renderCanvas({ ...DEFAULT_CHART_CONFIG, chartType: "bar", field: null });
    expect(screen.getByText(MESSAGE)).toBeInTheDocument();
  });
});
