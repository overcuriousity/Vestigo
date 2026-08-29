/**
 * The field-first rail: Field → Treat as → Figure → what the figure asks for.
 * Everything below is rendered from the registry (`CHART_META[c].inputs`),
 * so the assertions here pin the contract the registry makes with the rail.
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { ChartRail, type ChartRailProps } from "@/components/viz/ChartRail";
import {
  DEFAULT_CHART_CONFIG,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";
import { resolveChartOptions } from "@/components/viz/lib/chartOptions";
import { CHART_META, INPUT_KEYS } from "@/components/viz/lib/chartMeta";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    savedChartsApi: {
      ...actual.savedChartsApi,
      list: vi.fn().mockResolvedValue({ charts: [] }),
    },
  };
});

function renderRail(config: ChartConfig, over: Partial<ChartRailProps> = {}) {
  const updateConfig = vi.fn();
  const setAutoNotice = vi.fn();
  const props: ChartRailProps = {
    caseId: "c1",
    timelineId: "t1",
    timelineName: "demo",
    explorerHref: "/cases/c1/timelines/t1",
    config,
    updateConfig,
    fields: [
      { token: "artifact", distinct: 12, coverage: 0.98 },
      { token: "attr:bytes", distinct: 900, coverage: 0.7 },
    ],
    resolved: resolveChartOptions(config),
    autoBinCount: undefined,
    autoNotice: null,
    setAutoNotice,
    chartRefLive: false,
    brokenChartRef: null,
    droppedScope: null,
    corrMethod: "pearson",
    setCorrMethod: vi.fn(),
    metricAvailable: () => true,
    currentFilters: {},
    onLoadSavedChart: vi.fn(),
    svgRef: { current: null },
    exportFilename: "x",
    captionLines: [],
    ...over,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>
          <ChartRail {...props} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { updateConfig, setAutoNotice };
}

/** Section labels in DOM order. */
function sectionOrder(): string[] {
  return [...document.querySelectorAll("[data-rail-section]")].map(
    (el) => el.getAttribute("data-rail-section") ?? "",
  );
}

describe("ChartRail", () => {
  it("reads Field → Treat as → Figure, then the figure's inputs, then Compare", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "pivot",
      field: "artifact",
      scale: "nominal",
    });
    expect(sectionOrder().slice(0, 6)).toEqual([
      "header",
      "field",
      "scale",
      "figure",
      "secondField",
      "compare",
    ]);
  });

  it("offers 'No field — count every event' first, so the top control is never inert", () => {
    renderRail({ ...DEFAULT_CHART_CONFIG });
    const combo = screen.getByRole("combobox", { name: "Field" });
    fireEvent.focus(combo);
    const options = screen.getAllByRole("option");
    expect(options[0].textContent).toMatch(/count every event/i);
  });

  it("labels the scales in plain language and keeps the Stevens term for the tooltip", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "artifact",
    });
    const group = screen.getByRole("group", { name: "Treat as" });
    expect(
      within(group).getByRole("radio", { name: "Categories" }),
    ).toBeTruthy();
    expect(within(group).getByRole("radio", { name: "Measure" })).toBeTruthy();
    expect(within(group).queryByText(/nominal/)).toBeNull();
  });

  it("renders every figure as a thumbnail, greyed ones with their reason", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "artifact",
      scale: "nominal",
    });
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    const items = within(gallery).getAllByRole("radio");
    expect(items).toHaveLength(Object.keys(CHART_META).length);
    const histogram = within(gallery).getByRole("radio", { name: "Histogram" });
    expect(histogram.getAttribute("aria-disabled")).toBe("true");
    expect(histogram.getAttribute("title")).toBe(
      "Histogram needs Number or time or Measure.",
    );
  });

  it("shows the figure's question under the gallery", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "artifact",
      scale: "nominal",
    });
    expect(screen.getByText(CHART_META.bar.question)).toBeTruthy();
  });

  it("re-picks the figure when a scale change makes it illegal, and says so", () => {
    const { updateConfig, setAutoNotice } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    });
    fireEvent.click(screen.getByRole("radio", { name: "Categories" }));
    expect(updateConfig).toHaveBeenCalledWith({
      scale: "nominal",
      chartType: "bar",
    });
    expect(setAutoNotice).toHaveBeenCalledWith(
      "Figure switched to Bar — Histogram isn't available for a field treated as categories.",
    );
  });

  it("renders exactly the inputs the registry declares, for every figure", () => {
    const renderers: Record<string, RegExp> = {
      field: /^Field( \(X\))?$/,
      secondField: /^(Field \(Y\)|Group by \(optional\))$/,
      fields: /^Fields to correlate/,
    };
    for (const chartType of Object.keys(
      CHART_META,
    ) as (keyof typeof CHART_META)[]) {
      document.body.innerHTML = "";
      const meta = CHART_META[chartType];
      renderRail({
        ...DEFAULT_CHART_CONFIG,
        chartType,
        scale: meta.defaultScale,
        field: meta.inputs.field ? "artifact" : null,
        fieldY: meta.inputs.secondField === "required" ? "attr:bytes" : null,
      });
      for (const key of INPUT_KEYS) {
        const declared = key in meta.inputs;
        const renderer = renderers[key];
        if (!renderer) {
          // Vocabulary keys whose figures land in later plans: nothing may
          // render them yet, and no current figure declares them.
          expect(declared, `${chartType} declares ${key}`).toBe(false);
          continue;
        }
        const present = [...document.querySelectorAll("label")].some((l) =>
          renderer.test(l.textContent?.trim() ?? ""),
        );
        // `Field` is always rendered — the field-free figures show it with the
        // "count every event" entry selected — so it counts as present there.
        expect(
          present,
          `${chartType}: ${key} rendered=${present} declared=${declared}`,
        ).toBe(declared || key === "field");
      }
    }
  });
});
