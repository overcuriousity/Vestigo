/**
 * useMarkNormal optimistic cache filtering (Bug: marking a value normal left
 * the anomaly panel stale). The hook only filters cached anomalies queries
 * whose key[3] equals the target detector id — so every analysis view MUST
 * use the backend detector id (not a UI slug) at index 3 of its query key.
 * These tests pin the hook behavior against the exact keys the views use.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMarkNormal } from "@/hooks/useMarkNormal";
import type { AnomaliesResponse } from "@/api/types";

vi.mock("@/api/baselines", () => ({
  baselinesApi: { addAllowlist: vi.fn().mockResolvedValue({}) },
}));
vi.mock("@/api/annotations", () => ({
  annotationsApi: { create: vi.fn().mockResolvedValue({}) },
}));
vi.mock("@/stores/toasts", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const CASE = "c1";
const TL = "t1";

/** The exact query keys the analysis views use (key[3] = backend detector id). */
const VIEW_KEYS: Record<string, unknown[]> = {
  value_novelty: ["anomalies", CASE, TL, "value_novelty", "bl", "__auto__"],
  numeric_range: ["anomalies", CASE, TL, "numeric_range", "bl", "__auto__"],
  value_combo: ["anomalies", CASE, TL, "value_combo", "bl", "__auto__"],
  timestamp_order: ["anomalies", CASE, TL, "timestamp_order", 0],
  charset: ["anomalies", CASE, TL, "charset", "bl", "__auto__"],
  entropy: ["anomalies", CASE, TL, "entropy", "bl", "__auto__"],
  frequency: ["anomalies", CASE, TL, "frequency", "host", 3, "bl"],
  proportion_shift: ["anomalies", CASE, TL, "proportion_shift", "bl", "__auto__"],
  interval_periodicity: ["anomalies", CASE, TL, "interval_periodicity", "bl", "__auto__"],
};

function response(field: string, value: string): AnomaliesResponse {
  return {
    results: [
      {
        event_id: "ev1",
        details: { allowlist_field: field, allowlist_value: value },
      },
      {
        event_id: "ev2",
        details: { allowlist_field: field, allowlist_value: "other" },
      },
    ],
    total_findings: 10,
  } as unknown as AnomaliesResponse;
}

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useMarkNormal(CASE, TL), { wrapper });
  return { qc, result };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useMarkNormal optimistic filtering", () => {
  it.each(Object.keys(VIEW_KEYS).filter((d) => d !== "timestamp_order"))(
    "removes the finding from the %s view cache immediately",
    async (detector) => {
      const { qc, result } = setup();
      qc.setQueryData(VIEW_KEYS[detector], response("host", "evil"));

      result.current.mutate({ detector, field: "host", value: "evil" });

      await waitFor(() => {
        const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS[detector]);
        expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
        // The "N of M findings" bar must track the removal.
        expect(data?.total_findings).toBe(9);
      });
    },
  );

  it("leaves other detectors' caches untouched for a detector-scoped entry", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));
    qc.setQueryData(VIEW_KEYS.entropy, response("host", "evil"));

    result.current.mutate({ detector: "value_novelty", field: "host", value: "evil" });

    await waitFor(() => {
      expect(
        qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results,
      ).toHaveLength(1);
    });
    expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.entropy)?.results).toHaveLength(2);
  });

  it("filters every value-detector cache for a wildcard (*) entry", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));
    qc.setQueryData(VIEW_KEYS.charset, response("host", "evil"));

    result.current.mutate({ detector: "*", field: "host", value: "evil" });

    await waitFor(() => {
      expect(
        qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results,
      ).toHaveLength(1);
      expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.charset)?.results).toHaveLength(1);
    });
  });

  it("removes a positional finding by event id (timestamp_order fallback)", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.timestamp_order, response("host", "evil"));

    result.current.mutate({ detector: "timestamp_order", sourceId: "s1", eventId: "ev1" });

    await waitFor(() => {
      const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.timestamp_order);
      expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
    });
  });

  it("rolls the cache back when the write fails", async () => {
    const { baselinesApi } = await import("@/api/baselines");
    vi.mocked(baselinesApi.addAllowlist).mockRejectedValueOnce(new Error("nope"));
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));

    result.current.mutate({ detector: "value_novelty", field: "host", value: "evil" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results).toHaveLength(2);
  });
});
