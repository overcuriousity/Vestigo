/**
 * VisualizePage control rail — cause before effect (#298), field first.
 *
 * The rail once read Field → Scale → Chart type while the *dependency* ran
 * the other way, and two controls moved on their own without a word. It now
 * reads Field → Treat as → Figure: the field is what the analyst has, "treat
 * as" says what it is, and the gallery lights the figures that pair admits.
 *
 * These pin the contract: the order states the dependency, the field-free
 * figures are reachable from the field picker itself, and every automatic
 * re-pick says what it did.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
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
const fieldNumericMock = vi.fn();
const fieldTermsMock = vi.fn();
const dispositionsListMock = vi.fn();

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: {
      ...actual.vizApi,
      fields: (...a: unknown[]) => fieldsMock(...a),
      fieldNumeric: (...a: unknown[]) => fieldNumericMock(...a),
      fieldTerms: (...a: unknown[]) => fieldTermsMock(...a),
    },
  };
});

// `scopeReady` — and therefore the numeric probe — waits on the dispositions
// query, so this has to resolve or no automatic re-pick ever fires.
vi.mock("@/api/dispositions", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/dispositions")>(
      "@/api/dispositions",
    );
  return {
    ...actual,
    dispositionsApi: {
      ...actual.dispositionsApi,
      list: (...a: unknown[]) => dispositionsListMock(...a),
    },
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [
    { token: "artifact", distinct: 12, coverage: 0.98 },
    { token: "attr:src_port", distinct: 400, coverage: 0.7 },
  ],
};

/** The landing state the issue describes: nominal scale, Time histogram — the
 * one chart type that makes the field picker meaningless. */
const LANDING = "/cases/c1/timelines/t1/visualize";

function renderPage(entry = LANDING) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[entry]}>
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

/** Rail block labels in the order they appear in the DOM. */
function railOrder(): string[] {
  return [...document.querySelectorAll("label")]
    .map((el) => el.textContent?.trim() ?? "")
    .filter((t) => /^(Field|Treat as|Figure)$/.test(t));
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
  dispositionsListMock.mockResolvedValue({ dispositions: [] });
});

describe("VisualizePage rail order", () => {
  it("reads field first, then what to treat it as, then the figure", async () => {
    renderPage();
    await waitFor(() => expect(railOrder().length).toBeGreaterThan(0));

    const order = railOrder();
    const field = order.indexOf("Field");
    const scale = order.indexOf("Treat as");
    const figure = order.indexOf("Figure");

    // The field is what the analyst has; "treat as" says what it is; the
    // figure gallery lights what that pair admits. Read top-down, that is a
    // sentence — and the topmost control is never inert.
    expect(field).toBeGreaterThanOrEqual(0);
    expect(field).toBeLessThan(scale);
    expect(scale).toBeLessThan(figure);
  });
});

describe("VisualizePage field picker when the chart charts no field", () => {
  it("says why it is inert instead of just greying out", async () => {
    renderPage();
    // The landing chart type is the time histogram, which counts every event.
    expect(await screen.findByText(/counts every event/i)).toBeInTheDocument();
  });

  it("picking a field is the way out, and the rail says which figure it landed on", async () => {
    renderPage();
    const combo = await screen.findByRole("combobox", { name: /^Field/ });
    fireEvent.focus(combo);
    fireEvent.mouseDown(
      await screen.findByRole("option", { name: /artifact/ }),
    );

    // The field-free figure is left behind for one that charts the field —
    // an automatic move, so it names itself.
    expect(await screen.findByText(/figure set to bar/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/counts every event/i)).not.toBeInTheDocument(),
    );
  });
});

describe("VisualizePage automatic re-picks", () => {
  it("says so when changing the scale forces a new chart type", async () => {
    // Start on a chart type that is legal for nominal but not for ratio.
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=bar&c_scale=nominal&c_field=artifact",
    );
    await screen.findByRole("combobox", { name: /^Field/ });

    fireEvent.click(screen.getByRole("radio", { name: "Measure" }));

    expect(await screen.findByText(/figure switched to/i)).toBeInTheDocument();
  });

  it("says so when the numeric probe reassigns scale and chart type", async () => {
    fieldNumericMock.mockResolvedValue({
      field: "attr:src_port",
      count: 400,
      min: 1,
      max: 65535,
      mean: 100,
      stddev: 10,
      quantiles: {},
      bins: [],
    });
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=bar&c_scale=nominal&c_field=artifact",
    );
    const combo = await screen.findByRole("combobox", { name: /^Field/ });

    fireEvent.change(combo, { target: { value: "attr:src_port" } });
    fireEvent.keyDown(combo, { key: "Enter" });

    // Two controls moved on their own; the analyst is told which and why.
    expect(await screen.findByText(/looks numeric/i)).toBeInTheDocument();
  });

  it("drops the explanation once the analyst picks a chart type themselves", async () => {
    renderPage(
      "/cases/c1/timelines/t1/visualize?c_type=bar&c_scale=nominal&c_field=artifact",
    );
    await screen.findByRole("combobox", { name: /^Field/ });
    fireEvent.click(screen.getByRole("radio", { name: "Measure" }));
    await screen.findByText(/figure switched to/i);

    // An explicit choice supersedes the explanation of the automatic one.
    // Another "treat as" chip would not do: that is a second automatic
    // re-pick, which correctly replaces the notice rather than clearing it.
    // Any lit figure other than the one already selected — re-picking the
    // current value fires no change and would prove nothing.
    const gallery = screen.getByRole("radiogroup", { name: "Figure" });
    const other = within(gallery)
      .getAllByRole("radio")
      .find(
        (o) =>
          o.getAttribute("aria-checked") !== "true" &&
          o.getAttribute("aria-disabled") !== "true",
      )!;
    fireEvent.click(other);

    await waitFor(() =>
      expect(screen.queryByText(/figure switched to/i)).not.toBeInTheDocument(),
    );
  });
});
