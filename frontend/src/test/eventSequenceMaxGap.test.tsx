/**
 * D14 frontend: the sequence views expose max_gap_seconds and the charset
 * view exposes group_field — sent only when set, and part of the query key.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { EventSequenceView } from "@/components/analysis/EventSequenceView";
import { useBaselineStore } from "@/stores/baseline";
import { anomaliesApi } from "@/api/anomalies";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";

const listMock = vi.fn().mockResolvedValue({
  status: "ok",
  detector: "sequence_novelty",
  method: "ngram",
  results: [],
  total_findings: 0,
  dismissed_count: 0,
  baseline_size: 0,
});
const fieldsMock = vi.fn().mockResolvedValue({ fields: [] });

vi.mock("@/api/anomalies", async () => {
  const actual = await vi.importActual<typeof import("@/api/anomalies")>("@/api/anomalies");
  return {
    ...actual,
    anomaliesApi: {
      ...actual.anomaliesApi,
      list: (...args: unknown[]) => listMock(...args),
      fields: (...args: unknown[]) => fieldsMock(...args),
    },
  };
});

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <EventSequenceView caseId="c1" timelineId="t1" onSelectEvent={vi.fn()} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // sequence_novelty is temporal-only: an active baseline definition puts the
  // store into the baseline frame, which enables the view's query.
  useBaselineStore.getState().setActiveBaselineId("bl-1");
});

describe("EventSequenceView max-gap (D14)", () => {
  it("defaults to no gap bound — max_gap_seconds is not sent", async () => {
    renderView();
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const params = listMock.mock.calls.at(-1)?.[2];
    expect(params).toMatchObject({ detector: "sequence_novelty" });
    expect(params).not.toHaveProperty("max_gap_seconds");
  });

  it("sends max_gap_seconds when a bound is selected", async () => {
    renderView();
    fireEvent.change(await screen.findByTestId("max-gap-select"), { target: { value: "300" } });
    await waitFor(() => {
      const params = listMock.mock.calls.at(-1)?.[2];
      expect(params).toMatchObject({ detector: "sequence_novelty", max_gap_seconds: 300 });
    });
  });
});
