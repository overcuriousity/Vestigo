/**
 * The two Top-values controls on the Visualize rail, and the three ways they
 * used to disagree with the chart they drive (#297).
 *
 * The slider covers the common range and the number box beside it is the
 * escape hatch up to the chart type's ceiling, so the two are routinely out of
 * step by design — a bar chart's slider stops at 50 while a typed value may be
 * 500. What is pinned here is that the disagreement never becomes silent: a
 * gesture that looks like it moved the value has to move it, and a keystroke
 * that has not finished must not spend a ClickHouse scan on its way through.
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
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
const savedChartsMock = vi.fn();
const dispositionsMock = vi.fn();

vi.mock("@/api/dispositions", async () => {
  const actual = await vi.importActual<typeof import("@/api/dispositions")>("@/api/dispositions");
  return {
    ...actual,
    dispositionsApi: { ...actual.dispositionsApi, list: (...a: unknown[]) => dispositionsMock(...a) },
  };
});

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: {
      ...actual.vizApi,
      fields: (...a: unknown[]) => fieldsMock(...a),
      fieldTerms: (...a: unknown[]) => fieldTermsMock(...a),
    },
    savedChartsApi: { ...actual.savedChartsApi, list: (...a: unknown[]) => savedChartsMock(...a) },
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [{ token: "artifact", distinct: 120, coverage: 0.98 }],
};

/** A bar chart on a nominal field: slider ceiling 50, hard ceiling 500. */
const entryWithTopN = (topN: number) =>
  `/cases/c1/timelines/t1/visualize?c_type=bar&c_scale=nominal&c_field=artifact&c_opts=${encodeURIComponent(
    JSON.stringify({ topN }),
  )}`;

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
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

beforeEach(() => {
  vi.clearAllMocks();
  fieldsMock.mockResolvedValue(FIELDS);
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

afterEach(() => {
  vi.useRealTimers();
});

const slider = () => screen.getByRole("slider", { name: "Top values" });
const exactBox = () => screen.getByRole("spinbutton", { name: /top values \(exact\)/i });

describe("Top-values slider above its own ceiling", () => {
  it("commits a drag that ends on the ceiling", async () => {
    // With a typed 300 the thumb already sits at the slider's 50, so a drag
    // that ends there changes nothing in the DOM by the time it is released.
    // The release is what says "this is my answer" — and the movement on the
    // way to it is what makes the release a gesture rather than a click.
    renderPage(entryWithTopN(300));
    await waitFor(() => expect(screen.getByText("Top values: 300")).toBeTruthy());
    expect((slider() as HTMLInputElement).value).toBe("50");

    fireEvent.pointerDown(slider());
    fireEvent.change(slider(), { target: { value: "30" } });
    fireEvent.change(slider(), { target: { value: "50" } });
    fireEvent.pointerUp(slider());
    await waitFor(() => expect(screen.getByText("Top values: 50")).toBeTruthy());
  });

  it("leaves the value alone when the pinned thumb is clicked but not moved", async () => {
    // A press and release on the pinned thumb moved nothing: no `change` event
    // fired. Committing the thumb's clamped 50 there would rewrite a typed 300
    // — and spend a gated ClickHouse scan — for a gesture that expressed
    // nothing.
    renderPage(entryWithTopN(300));
    await waitFor(() => expect(screen.getByText("Top values: 300")).toBeTruthy());

    fireEvent.pointerDown(slider());
    fireEvent.pointerUp(slider());
    expect(screen.getByText("Top values: 300")).toBeTruthy();
  });

  it("commits the ceiling when a movement key lands there", async () => {
    renderPage(entryWithTopN(300));
    await waitFor(() => expect(screen.getByText("Top values: 300")).toBeTruthy());

    fireEvent.keyUp(slider(), { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByText("Top values: 50")).toBeTruthy());
  });

  it("leaves the value alone when the slider is merely focused", async () => {
    // Tabbing *into* the slider fires keyup on it, and the clamped thumb reads
    // 50 — committing there would rewrite 300 for a keystroke that moved
    // nothing.
    renderPage(entryWithTopN(300));
    await waitFor(() => expect(screen.getByText("Top values: 300")).toBeTruthy());

    fireEvent.keyUp(slider(), { key: "Tab" });
    fireEvent.focus(slider());
    expect(screen.getByText("Top values: 300")).toBeTruthy();
  });
});

describe("Top-values slider cost", () => {
  it("spends one request on a drag, not one per step it passes through", async () => {
    // Same reason the box beside it debounces: every commit re-runs a gated
    // ClickHouse foreground scan. Committing on `change` made one drag across
    // the slider's travel dozens of them.
    renderPage(entryWithTopN(40));
    await waitFor(() =>
      expect([...new Set(fieldTermsMock.mock.calls.map((c) => c[4]))]).toEqual([40]),
    );

    fireEvent.pointerDown(slider());
    for (const n of ["30", "20", "12", "10"]) {
      fireEvent.change(slider(), { target: { value: n } });
    }
    // The label tracks the thumb the whole way — the draft is live, only the
    // commit waits.
    expect(screen.getByText("Top values: 10")).toBeTruthy();
    expect([...new Set(fieldTermsMock.mock.calls.map((c) => c[4]))]).toEqual([40]);

    fireEvent.pointerUp(slider());
    await waitFor(() =>
      expect([...new Set(fieldTermsMock.mock.calls.map((c) => c[4]))]).toEqual([40, 10]),
    );
  });
});

describe("Top-values exact-value box", () => {
  it("spends one request on a multi-digit entry, not one per digit", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderPage(entryWithTopN(10));
    await waitFor(() => expect(screen.getByText("Top values: 10")).toBeTruthy());

    fireEvent.change(exactBox(), { target: { value: "5" } });
    fireEvent.change(exactBox(), { target: { value: "50" } });
    fireEvent.change(exactBox(), { target: { value: "500" } });
    // Nothing committed yet — "5" and "50" are in range but unfinished.
    expect(screen.getByText("Top values: 10")).toBeTruthy();

    await vi.advanceTimersByTimeAsync(500);
    await waitFor(() => expect(screen.getByText("Top values: 500")).toBeTruthy());
    // One request for the whole entry, not one per digit: without the debounce
    // this was 5, 50 and 500, each a gated foreground scan.
    // The whole entry costs one request, not one per digit: undebounced, the
    // in-range prefixes 5 and 50 each spent a gated foreground scan on their
    // way to 500.
    await waitFor(() =>
      expect([...new Set(fieldTermsMock.mock.calls.map((c) => c[4]))]).toEqual([10, 500]),
    );
  });

  it("commits and clamps on Enter, rather than waiting for a click elsewhere", async () => {
    // 900 is past the bar ceiling of 500, so it is deliberately not committed
    // while typing. Without Enter it sat on screen, uncommitted and unclamped.
    renderPage(entryWithTopN(10));
    await waitFor(() => expect(screen.getByText("Top values: 10")).toBeTruthy());

    fireEvent.change(exactBox(), { target: { value: "900" } });
    fireEvent.keyDown(exactBox(), { key: "Enter" });
    await waitFor(() => expect(screen.getByText("Top values: 500")).toBeTruthy());
  });
});
