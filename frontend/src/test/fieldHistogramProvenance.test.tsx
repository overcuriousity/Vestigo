/**
 * Where the top-values list's answer came from, and what changes when it
 * changes (#296).
 *
 * An unfiltered first page is served from the per-source `field_stats` cache:
 * per-value counts are exact sums, but the cross-source top-N merge is
 * approximate and `distinct` is a max-across-sources. That cache answers
 * nothing above 50, so the first "+ N more" click falls through to an exact
 * live scan — which on a multi-source timeline may reorder the values already
 * on screen and change their counts. Rows moving under an analyst is fine;
 * rows moving with nothing on screen saying why is not.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { FieldHistogramModal } from "@/components/viz/FieldHistogramModal";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";
import type { FieldTermsResponse } from "@/api/types";

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

const fieldTermsMock = vi.fn();

vi.mock("@/api/events", async () => {
  const actual = await vi.importActual<typeof import("@/api/events")>("@/api/events");
  return {
    ...actual,
    eventsApi: {
      ...actual.eventsApi,
      histogram: async () => ({ buckets: [], interval_seconds: 60, min: null, max: null }),
    },
  };
});

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: { ...actual.vizApi, fieldTerms: (...a: unknown[]) => fieldTermsMock(...a) },
  };
});

/** 50 values plus a tail, so the "+ N more" expander renders. */
const page = (cached: boolean, limit: number): FieldTermsResponse => ({
  field: "artifact",
  total: 1000,
  distinct: cached ? 120 : 137,
  other_count: 400,
  values: Array.from({ length: Math.min(limit, 60) }, (_, i) => ({
    value: `v${i}`,
    count: 100 - i,
  })),
  ...(cached ? { cached: true } : {}),
});

function renderModal() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <FieldHistogramModal
          open
          onOpenChange={() => {}}
          caseId="c1"
          timelineId="t1"
          filters={{}}
          fieldKey="artifact"
          value="v0"
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  fieldTermsMock.mockImplementation(async (..._args: unknown[]) => {
    const limit = _args[4] as number;
    return page(limit <= 50, limit);
  });
});

describe("top-values provenance", () => {
  it("names the cache while the list is served from it", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText(/per-source summary cache/i)).toBeTruthy());
    // The distinct total is a max-across-sources there, and says so.
    expect(screen.getByText(/≈120 distinct/)).toBeTruthy();
  });

  it("says the list was re-read once expanding crosses to a live scan", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText(/per-source summary cache/i)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /more in other values/i }));

    await waitFor(() => expect(screen.getByText(/Re-read from the events/i)).toBeTruthy());
    expect(screen.queryByText(/per-source summary cache/i)).toBeNull();
    expect(screen.getByText(/137 distinct/)).toBeTruthy();
  });

  it("stays silent when the first answer was already live", async () => {
    fieldTermsMock.mockImplementation(async (..._args: unknown[]) =>
      page(false, _args[4] as number),
    );
    renderModal();
    await waitFor(() => expect(screen.getByText(/137 distinct/)).toBeTruthy());
    expect(screen.queryByText(/per-source summary cache/i)).toBeNull();
    expect(screen.queryByText(/Re-read from the events/i)).toBeNull();
  });
});
