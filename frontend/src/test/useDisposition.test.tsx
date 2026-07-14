/**
 * useDisposition optimistic cache filtering (successor of the useMarkNormal
 * regression suite for the "marking a value normal left the anomaly panel
 * stale" bug). The hook only filters cached anomalies queries whose key[3]
 * equals the target detector id — so every analysis view MUST use the backend
 * detector id (not a UI slug) at index 3 of its query key. These tests pin
 * the hook behavior against the exact keys the views use, across all three
 * disposition kinds.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useDisposition } from "@/hooks/useDisposition";
import type { AnomaliesResponse } from "@/api/types";

vi.mock("@/api/dispositions", () => ({
  dispositionsApi: {
    create: vi.fn().mockResolvedValue({ disposition: { id: "d1" } }),
    remove: vi.fn().mockResolvedValue({ deleted: true, disposition_id: "d1" }),
  },
}));
vi.mock("@/api/anomalies", () => ({
  anomaliesApi: { persistFinding: vi.fn().mockResolvedValue({}) },
}));
vi.mock("@/stores/toasts", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const CASE = "c1";
const TL = "t1";

/**
 * The exact query keys the analysis views use (key[3] = backend detector id;
 * the "dismissed-hidden"/"dismissed-shown" segment is useShowDismissed's
 * keyPart — useDisposition recognizes revealed caches by that named segment).
 */
const VIEW_KEYS: Record<string, unknown[]> = {
  value_novelty: ["anomalies", CASE, TL, "value_novelty", "bl", "__auto__", 50, "dismissed-hidden"],
  numeric_range: ["anomalies", CASE, TL, "numeric_range", "bl", "__auto__", 50, "dismissed-hidden"],
  value_combo: ["anomalies", CASE, TL, "value_combo", "bl", "__auto__", 50, "dismissed-hidden"],
  timestamp_order: ["anomalies", CASE, TL, "timestamp_order", 0, 100, "dismissed-hidden"],
  charset: ["anomalies", CASE, TL, "charset", "bl", "__auto__", 50, "dismissed-hidden"],
  entropy: ["anomalies", CASE, TL, "entropy", "bl", "__auto__", 50, "dismissed-hidden"],
  frequency: ["anomalies", CASE, TL, "frequency", "host", 3, "bl", 30, "dismissed-hidden"],
  proportion_shift: ["anomalies", CASE, TL, "proportion_shift", "bl", "__auto__", 50, "dismissed-hidden"],
  interval_periodicity: ["anomalies", CASE, TL, "interval_periodicity", "bl", "__auto__", 50, "dismissed-hidden"],
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
  const { result } = renderHook(() => useDisposition(CASE, TL), { wrapper });
  return { qc, result };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useDisposition optimistic filtering", () => {
  it.each(Object.keys(VIEW_KEYS).filter((d) => d !== "timestamp_order"))(
    "normal removes the finding from the %s view cache immediately",
    async (detector) => {
      const { qc, result } = setup();
      qc.setQueryData(VIEW_KEYS[detector], response("host", "evil"));

      result.current.mutate({ kind: "normal", detector, field: "host", value: "evil" });

      await waitFor(() => {
        const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS[detector]);
        expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
        // The "N of M findings" bar must track the removal.
        expect(data?.total_findings).toBe(9);
      });
    },
  );

  it("dismissed removes the finding and bumps dismissed_count", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));

    result.current.mutate({
      kind: "dismissed",
      detector: "value_novelty",
      field: "host",
      value: "evil",
    });

    await waitFor(() => {
      const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty);
      expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
      expect(data?.dismissed_count).toBe(1);
    });
  });

  it("dismissed flags (not removes) in a cache fetched with the show-dismissed toggle", async () => {
    const { qc, result } = setup();
    const key = [...VIEW_KEYS.value_novelty.slice(0, -1), "dismissed-shown"];
    qc.setQueryData(key, response("host", "evil"));

    result.current.mutate({
      kind: "dismissed",
      detector: "value_novelty",
      field: "host",
      value: "evil",
    });

    await waitFor(() => {
      const data = qc.getQueryData<AnomaliesResponse>(key);
      // Row stays, flagged; total_findings untouched.
      expect(data?.results.map((f) => [f.event_id, f.dismissed ?? false])).toEqual([
        ["ev1", true],
        ["ev2", false],
      ]);
      expect(data?.total_findings).toBe(10);
      expect(data?.dismissed_count).toBe(1);
    });
  });

  it("normal still removes even in a show-dismissed cache", async () => {
    const { qc, result } = setup();
    const key = [...VIEW_KEYS.value_novelty.slice(0, -1), "dismissed-shown"];
    qc.setQueryData(key, response("host", "evil"));

    result.current.mutate({ kind: "normal", detector: "value_novelty", field: "host", value: "evil" });

    await waitFor(() => {
      const data = qc.getQueryData<AnomaliesResponse>(key);
      expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
    });
  });

  it("confirmed calls the persist endpoint and flags (not removes) the finding", async () => {
    const { anomaliesApi } = await import("@/api/anomalies");
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.charset, response("host", "evil"));

    result.current.mutate({
      kind: "confirmed",
      detector: "charset",
      field: "host",
      value: "evil",
      sourceId: "s1",
      eventId: "ev1",
      content: "confirmed finding",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(anomaliesApi.persistFinding).toHaveBeenCalledWith(
      CASE,
      "s1",
      "ev1",
      expect.objectContaining({ detector: "charset", content: "confirmed finding" }),
    );
    // Row stays, optimistically flagged confirmed — the durable badge state.
    const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.charset);
    expect(data?.results.map((f) => [f.event_id, f.confirmed ?? false])).toEqual([
      ["ev1", true],
      ["ev2", false],
    ]);
  });

  it("leaves other detectors' caches untouched for a detector-scoped verdict", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));
    qc.setQueryData(VIEW_KEYS.entropy, response("host", "evil"));

    result.current.mutate({
      kind: "normal",
      detector: "value_novelty",
      field: "host",
      value: "evil",
    });

    await waitFor(() => {
      expect(
        qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results,
      ).toHaveLength(1);
    });
    expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.entropy)?.results).toHaveLength(2);
  });

  it("filters every value-detector cache for a wildcard (*) verdict", async () => {
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));
    qc.setQueryData(VIEW_KEYS.charset, response("host", "evil"));

    result.current.mutate({ kind: "normal", detector: "*", field: "host", value: "evil" });

    await waitFor(() => {
      expect(
        qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results,
      ).toHaveLength(1);
      expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.charset)?.results).toHaveLength(1);
    });
  });

  it("wildcard verdict survives non-findings caches under the anomalies prefix", async () => {
    // Regression: the ["anomalies", case, tl] prefix also holds the
    // field-inventory caches ("fields"/"numeric-fields"), whose payload has
    // no `results` array. A wildcard disposition used to crash on them
    // (`data.results.filter` on undefined), killing the mutation before the
    // API call ever fired.
    const { dispositionsApi } = await import("@/api/dispositions");
    const { qc, result } = setup();
    qc.setQueryData(["anomalies", CASE, TL, "fields"], { fields: [{ token: "attr:host" }] });
    qc.setQueryData(["anomalies", CASE, TL, "numeric-fields"], { fields: [] });
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));

    result.current.mutate({ kind: "normal", detector: "*", field: "host", value: "evil" });

    await waitFor(() => {
      expect(
        qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results,
      ).toHaveLength(1);
    });
    expect(dispositionsApi.create).toHaveBeenCalled();
    // Inventory caches pass through untouched.
    expect(qc.getQueryData(["anomalies", CASE, TL, "fields"])).toEqual({
      fields: [{ token: "attr:host" }],
    });
  });

  it("removes a positional finding by event id (timestamp_order)", async () => {
    const { dispositionsApi } = await import("@/api/dispositions");
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.timestamp_order, response("host", "evil"));

    result.current.mutate({
      kind: "normal",
      detector: "timestamp_order",
      sourceId: "s1",
      eventId: "ev1",
    });

    await waitFor(() => {
      const data = qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.timestamp_order);
      expect(data?.results.map((f) => f.event_id)).toEqual(["ev2"]);
    });
    expect(dispositionsApi.create).toHaveBeenCalledWith(
      CASE,
      TL,
      expect.objectContaining({ kind: "normal", source_id: "s1", event_id: "ev1" }),
    );
  });

  it("rolls the cache back when the write fails", async () => {
    const { dispositionsApi } = await import("@/api/dispositions");
    vi.mocked(dispositionsApi.create).mockRejectedValueOnce(new Error("nope"));
    const { qc, result } = setup();
    qc.setQueryData(VIEW_KEYS.value_novelty, response("host", "evil"));

    result.current.mutate({
      kind: "normal",
      detector: "value_novelty",
      field: "host",
      value: "evil",
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getQueryData<AnomaliesResponse>(VIEW_KEYS.value_novelty)?.results).toHaveLength(2);
  });
});
