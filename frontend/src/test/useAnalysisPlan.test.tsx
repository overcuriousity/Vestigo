/**
 * The gate's client side, and the one property that matters most about it:
 * a failure must never hide a method. The plan decides what runs *first* and
 * explains what it did not run; if it breaks, the panel has to degrade to the
 * old unconditional behavior rather than showing an analyst fewer methods with
 * no indication that it did.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAnalysisPlan } from "@/hooks/useAnalysisPlan";
import { analysisApi } from "@/api/analysis";
import { METHODS } from "@/components/analysis/method-registry";

vi.mock("@/api/analysis", () => ({
  analysisApi: { plan: vi.fn(), findings: vi.fn() },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useAnalysisPlan", () => {
  beforeEach(() => vi.resetAllMocks());

  it("indexes the plan by method id", async () => {
    vi.mocked(analysisApi.plan).mockResolvedValue({
      methods: [
        {
          method: "value_novelty",
          status: "applicable",
          reason: "",
          reason_facts: {},
          cost_class: "cheap",
        },
        {
          method: "numeric_range",
          status: "not_applicable",
          reason: "no field parses as numeric",
          reason_facts: { numeric_fields: 0, sampled: 19 },
          cost_class: "heavy",
        },
      ],
      scope: { frame: "self", baseline_id: null, baseline_name: null },
      events_total: 1000,
    });

    const { result } = renderHook(() => useAnalysisPlan("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.planById.numeric_range?.status).toBe("not_applicable");
    expect(result.current.planById.numeric_range?.reason_facts.sampled).toBe(19);
    expect(result.current.failedOpen).toBe(false);
  });

  it("fails open so a broken gate never hides a method", async () => {
    vi.mocked(analysisApi.plan).mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useAnalysisPlan("c1", "t1"), { wrapper });
    // The hook retries once before declaring the gate failed, so give the
    // retry room rather than weakening the hook to suit the test.
    await waitFor(() => expect(result.current.failedOpen).toBe(true), { timeout: 5000 });
    expect(Object.keys(result.current.planById)).toHaveLength(METHODS.length);
    for (const m of METHODS) {
      expect(result.current.planById[m.id].status).toBe("applicable");
    }
  });

  it("reports the scope the plan was produced under", async () => {
    vi.mocked(analysisApi.plan).mockResolvedValue({
      methods: [],
      scope: { frame: "baseline", baseline_id: "bl-1", baseline_name: "Feb 24 – Mar 1" },
      events_total: 10,
    });
    const { result } = renderHook(() => useAnalysisPlan("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.scope.baseline_name).toBe("Feb 24 – Mar 1");
  });
});
