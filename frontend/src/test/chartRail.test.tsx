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

vi.mock("@/api/baselines", () => ({
  baselinesApi: { list: vi.fn().mockResolvedValue({ baselines: [] }) },
}));
vi.mock("@/api/views", () => ({ viewsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: { list: vi.fn().mockResolvedValue({ dispositions: [] }) },
}));

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
      secondField: /^(Field \(Y\)|Group by \(optional\)|Count distinct of \(optional\))$/,
      fields: /^Fields to correlate/,
      columns: /^Columns$/,
      pairing: /^Pairing$/,
      startFilter: /^Start events$/,
      endFilter: /^End events$/,
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

describe("ChartRail — Derive", () => {
  it("offers Derive only where the treat-as admits one, between Treat as and Figure", () => {
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "histogram", field: "attr:bytes", scale: "ratio" });
    expect(sectionOrder().slice(1, 4)).toEqual(["field", "scale", "derive"]);
    document.body.innerHTML = "";
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "bar", field: "artifact", scale: "nominal" });
    expect(sectionOrder()).not.toContain("derive");
  });

  it("withholds Derive from a figure that would keep it and never send it", () => {
    // Cumulative is legal at every scale, so it stays selected under a
    // derivation — and its registry entry admits none, so the query would drop
    // it while the caption (and a Story export) still named the ranges.
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "cumulative",
      field: "attr:bytes",
      scale: "ratio",
    });
    expect(sectionOrder()).not.toContain("derive");
    expect(screen.queryByRole("radio", { name: "Group into ranges" })).toBeNull();
  });

  it("grouping into ranges makes the field ordered categories and lights the category figures", () => {
    const { updateConfig } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    });
    fireEvent.click(screen.getByRole("radio", { name: "Group into ranges" }));
    expect(updateConfig).toHaveBeenCalledWith({
      derive: { kind: "bins", mode: "log", count: 8 },
      chartType: "bar",
      options: { sort: "value" },
    });
  });

  it("with a derivation active, the gallery judges legality at the derived scale", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "attr:bytes",
      scale: "ratio",
      derive: { kind: "bins", mode: "log", count: 8 },
    });
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    expect(within(gallery).getByRole("radio", { name: "Bar" }).getAttribute("aria-disabled")).toBe("false");
    expect(within(gallery).getByRole("radio", { name: "Histogram" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText(/now treated as ordered categories/i)).toBeTruthy();
  });

  it("clicking a greyed figure applies the one derivation that lights it, and says so", () => {
    const { updateConfig, setAutoNotice } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    });
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    fireEvent.click(within(gallery).getByRole("radio", { name: "Bar" }));
    expect(updateConfig).toHaveBeenCalledWith({
      chartType: "bar",
      derive: { kind: "bins", mode: "log", count: 8 },
      options: { sort: "value" },
    });
    expect(setAutoNotice).toHaveBeenCalledWith(
      "Bar needs categories — grouped attr:bytes into 8 log-spaced ranges.",
    );
  });

  it("leaves a greyed figure alone when two derivations could light it", () => {
    const { updateConfig } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "heatmap",
      field: "attr:logon_at",
      scale: "interval",
      // heatmap is legal at interval; pie is not, and admits no derivation at all
    });
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    fireEvent.click(within(gallery).getByRole("radio", { name: "Pie / Donut" }));
    expect(updateConfig).not.toHaveBeenCalled();
  });

  it("changing treat-as to categories drops a derivation that no longer applies", () => {
    const { updateConfig } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "attr:bytes",
      scale: "ratio",
      derive: { kind: "bins", mode: "log", count: 8 },
    });
    fireEvent.click(screen.getByRole("radio", { name: "Categories" }));
    expect(updateConfig).toHaveBeenCalledWith({ scale: "nominal", derive: null });
  });
});

describe("ChartRail — table", () => {
  const table = {
    ...DEFAULT_CHART_CONFIG,
    chartType: "table" as const,
    field: "attr:user",
    scale: "nominal" as const,
  };

  it("renders the Columns checklist after the second field, with distinct disabled until a second field is set", () => {
    renderRail(table);
    const order = sectionOrder();
    expect(order.indexOf("columns")).toBe(order.indexOf("secondField") + 1);
    const group = screen.getByRole("group", { name: "Columns" });
    const distinct = within(group).getByRole("checkbox", { name: /distinct/ }) as HTMLInputElement;
    expect(distinct.disabled).toBe(true);
    expect(
      (within(group).getByRole("checkbox", { name: "count" }) as HTMLInputElement).checked,
    ).toBe(true);
    expect(screen.getByRole("combobox", { name: "Count distinct of (optional)" })).toBeTruthy();
  });

  it("unticking a column writes the explicit column list", () => {
    const { updateConfig } = renderRail(table);
    fireEvent.click(
      within(screen.getByRole("group", { name: "Columns" })).getByRole("checkbox", {
        name: "first seen",
      }),
    );
    expect(updateConfig).toHaveBeenCalledWith({
      inputs: { columns: ["count", "share", "last_seen"] },
    });
  });

  it("offers sort, direction and highlight options and a Top values control", () => {
    const { updateConfig } = renderRail({ ...table, fieldY: "attr:host" });
    expect(screen.getByRole("combobox", { name: "Sort by" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Direction" })).toBeTruthy();
    expect(screen.getByLabelText("Top values (exact)")).toBeTruthy();
    // One value per line, so a value carrying a comma of its own — a DN, a
    // user-agent — is one highlighted value and not two that match nothing.
    const highlight = screen.getByRole("textbox", { name: "Highlight values" });
    fireEvent.change(highlight, { target: { value: "alice\nCN=bob,OU=staff" } });
    fireEvent.blur(highlight);
    expect(updateConfig).toHaveBeenCalledWith({
      options: { highlight: ["alice", "CN=bob,OU=staff"] },
    });
  });
});

describe("ChartRail — Compare", () => {
  it("drops a comparison the newly picked figure cannot draw, and says so", () => {
    // Left in the config it is invisible *and* unreachable — all three radios
    // render unchecked and disabled — while the caption names a comparison
    // layer that was never fetched.
    const { updateConfig, setAutoNotice } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "artifact",
      scale: "nominal",
      compare: { mode: "baseline" },
    });
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    fireEvent.click(within(gallery).getByRole("radio", { name: "Pie / Donut" }));
    expect(updateConfig).toHaveBeenCalledWith({
      chartType: "pie",
      compare: { mode: "off" },
    });
    expect(setAutoNotice).toHaveBeenCalledWith(
      "Pie / Donut charts one layer — the comparison was dropped.",
    );
  });
});

describe("ChartRail — marks", () => {
  it("renders the Marks section for figures that draw them, after Compare, and not otherwise", () => {
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "time", scale: "nominal" });
    const order = sectionOrder();
    // After Compare and its Metric (the two belong together: "% of baseline"),
    // before the per-chart options.
    expect(order.indexOf("marks")).toBeGreaterThan(order.indexOf("compare"));
    expect(order.indexOf("marks")).toBe(order.indexOf("metric") + 1);
    expect(screen.getByRole("combobox", { name: "Add mark" })).toBeTruthy();
    document.body.innerHTML = "";
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "bar", field: "artifact", scale: "nominal" });
    expect(sectionOrder()).not.toContain("marks");
  });
});

describe("ChartRail — field-optional figures", () => {
  it("offers 'No field' on a cumulative and keeps the figure when a field is picked", () => {
    const { updateConfig } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "cumulative",
      field: null,
    });
    // The optional hint, not the field-free reason.
    expect(screen.getByText(/without a field this figure counts every event/i)).toBeTruthy();
    const combo = screen.getByRole("combobox", { name: "Field" });
    fireEvent.focus(combo);
    fireEvent.mouseDown(screen.getByRole("option", { name: /^artifact/ }));
    expect(updateConfig).toHaveBeenLastCalledWith(expect.objectContaining({ field: "artifact" }));
    expect(updateConfig).not.toHaveBeenCalledWith(
      expect.objectContaining({ chartType: expect.anything() }),
    );
  });

  it("renders the Quantity select for cumulative and greys the dishonest choices", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "cumulative",
      field: "artifact",
      scale: "nominal",
    });
    fireEvent.click(screen.getByText("Options"));
    fireEvent.click(screen.getByRole("combobox", { name: "Quantity" }));
    expect(
      screen
        .getByRole("option", { name: "Distinct values seen so far" })
        .getAttribute("aria-disabled"),
    ).not.toBe("true");
    expect(
      screen.getByRole("option", { name: "Running sum (measure)" }).getAttribute("aria-disabled"),
    ).toBe("true");
  });
});

describe("ChartRail — a figure that requires Compare", () => {
  it("disables Compare's Off for ranked change and says why", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "change",
      field: "artifact",
      compare: { mode: "baseline" },
    });
    const off = screen.getByRole("radio", { name: "Off" }) as HTMLInputElement;
    expect(off.disabled).toBe(true);
    expect(screen.getByText(/needs two windows/i)).toBeTruthy();
    expect(
      (screen.getByRole("radio", { name: "Baseline (all events)" }) as HTMLInputElement).disabled,
    ).toBe(false);
  });

  it("switches Compare to Baseline when ranked change is picked with Compare off, and says so", () => {
    const { updateConfig, setAutoNotice } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar",
      field: "artifact",
      compare: { mode: "off" },
    });
    fireEvent.click(
      screen.getByRole("radio", { name: CHART_META.change.label }),
    );
    expect(updateConfig).toHaveBeenLastCalledWith({
      chartType: "change",
      compare: { mode: "baseline" },
    });
    expect(setAutoNotice).toHaveBeenLastCalledWith(expect.stringMatching(/Compare set to Baseline/));
  });

  it("renders the per-window top-N and the layout select under Options", () => {
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "change",
      field: "artifact",
      compare: { mode: "baseline" },
    });
    fireEvent.click(screen.getByText("Options"));
    expect(screen.getByText(/Top values per window: 10/)).toBeTruthy();
    fireEvent.click(screen.getByRole("combobox", { name: "Layout" }));
    expect(screen.getByRole("option", { name: "Slope (two columns)" })).toBeTruthy();
  });
});

describe("ChartRail — interval lanes inputs", () => {
  it("renders the pairing radios and the not-used notes under first-to-last", () => {
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "lanes", field: "artifact" });
    const first = screen.getByRole("radio", { name: /First to last/ }) as HTMLInputElement;
    expect(first.checked).toBe(true);
    expect(screen.getAllByText(/Not used — first-to-last pairing needs no filters/)).toHaveLength(2);
  });

  it("switches the pairing through inputs and shows both filter editors under next end", () => {
    const { updateConfig } = renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "lanes",
      field: "artifact",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Start → next end/ }));
    expect(updateConfig).toHaveBeenLastCalledWith({ inputs: { pairing: "nextEnd" } });
    document.body.innerHTML = "";
    renderRail({
      ...DEFAULT_CHART_CONFIG,
      chartType: "lanes",
      field: "artifact",
      inputs: { pairing: "nextEnd" },
    });
    expect(screen.queryByText(/Not used — first-to-last/)).toBeNull();
    expect(screen.getByText("Start events")).toBeTruthy();
    expect(screen.getByText("End events")).toBeTruthy();
  });

  it("renders the lane cap under Options", () => {
    renderRail({ ...DEFAULT_CHART_CONFIG, chartType: "lanes", field: "artifact" });
    fireEvent.click(screen.getByText("Options"));
    expect(screen.getByText(/Lanes: 10/)).toBeTruthy();
  });
});

describe("ChartRail — a figure pick and the active derivation", () => {
  const derived: ChartConfig = {
    ...DEFAULT_CHART_CONFIG,
    chartType: "bar",
    field: "attr:bytes",
    scale: "ratio",
    derive: { kind: "bins", mode: "log", count: 8 },
  };

  it("drops the derivation when the picked figure admits none, and says so", () => {
    const { updateConfig, setAutoNotice } = renderRail(derived);
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    // Legal at the derived (ordinal) scale, but `derives` is empty for it.
    fireEvent.click(within(gallery).getByRole("radio", { name: CHART_META.cumulative.label }));
    expect(updateConfig).toHaveBeenCalledWith({ chartType: "cumulative", derive: null });
    expect(setAutoNotice).toHaveBeenCalledWith(
      `${CHART_META.cumulative.label} charts attr:bytes as is — the derivation was dropped.`,
    );
  });

  it("keeps the derivation when the picked figure admits it", () => {
    const { updateConfig } = renderRail(derived);
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    fireEvent.click(within(gallery).getByRole("radio", { name: CHART_META.heatmap.label }));
    expect(updateConfig).toHaveBeenCalledWith({ chartType: "heatmap" });
  });

  it("drops the derivation with the field when 'No field' lands on the time histogram", () => {
    const { updateConfig } = renderRail(derived);
    const combo = screen.getByRole("combobox", { name: "Field" });
    fireEvent.focus(combo);
    fireEvent.mouseDown(screen.getByRole("option", { name: /No field/ }));
    expect(updateConfig).toHaveBeenCalledWith({ field: null, chartType: "time", derive: null });
  });
});
