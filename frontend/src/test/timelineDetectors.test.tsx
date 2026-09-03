/**
 * useTimelineDetectors — the configured list is shared server state on the
 * Timeline, read through the same ["timeline", case, timeline] query the
 * Explorer already holds, and every write lands straight in that cache so a
 * just-added detector starts fetching without a round trip.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTimelineDetectors, scopeOf } from "@/hooks/useTimelineDetectors";
import { useStreamingSweep } from "@/hooks/useMethodFindings";

const server = vi.hoisted(() => ({
  detectors: [] as Record<string, unknown>[],
  puts: [] as unknown[],
  deletes: [] as string[],
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({ id: "t1", case_id: "c1", detectors: server.detectors }),
    putDetector: async (_c: string, _t: string, method: string, body: unknown) => {
      server.puts.push({ method, body });
      const entry = {
        method,
        ...(body as object),
        added_by: "u1",
        added_at: "2026-09-03T00:00:00Z",
      };
      server.detectors = [...server.detectors.filter((d) => d.method !== method), entry];
      return { id: "t1", case_id: "c1", detectors: server.detectors };
    },
    deleteDetector: async (_c: string, _t: string, method: string) => {
      server.deletes.push(method);
      server.detectors = server.detectors.filter((d) => d.method !== method);
      return { id: "t1", case_id: "c1", detectors: server.detectors };
    },
  },
}));
vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access: "contribute" }) },
}));
vi.mock("@/lib/caseAccess", () => ({ canContributeToCase: () => true }));

const asked = vi.hoisted(() => ({ calls: [] as Record<string, unknown>[] }));
const plan = vi.hoisted(() => ({ status: "applicable" as string }));

vi.mock("@/api/analysis", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/analysis")>();
  const { METHODS } = await import("@/components/analysis/method-registry");
  return {
    ...actual,
    analysisApi: {
      plan: async (_c: string, _t: string, scope: { frame: string; baseline_id?: string }) => ({
        methods: METHODS.map((m) => ({
          method: m.id,
          status: plan.status,
          reason: "",
          reason_facts: {},
          cost_class: m.costClass,
        })),
        scope: { frame: scope.frame, baseline_id: scope.baseline_id ?? null, baseline_name: null },
        events_total: 1,
      }),
      findings: async (
        _c: string,
        _t: string,
        args: { method: string; frame: string; baseline_id?: string; params?: unknown },
      ) => {
        asked.calls.push({
          method: args.method,
          frame: args.frame,
          baseline_id: args.baseline_id,
          params: args.params,
        });
        return {
          method: args.method,
          results: [],
          total_findings: 0,
          dismissed_count: 0,
          warnings: [],
          scope: { frame: args.frame, baseline_id: args.baseline_id ?? null, baseline_name: null },
          cache: "miss",
        };
      },
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useTimelineDetectors", () => {
  beforeEach(() => {
    server.detectors = [];
    server.puts = [];
    server.deletes = [];
  });

  it("reads the timeline's list and drops methods this build does not know", async () => {
    server.detectors = [
      { method: "value_novelty", params: {}, frame: "self", baseline_id: null },
      { method: "from_the_future", params: {}, frame: "self", baseline_id: null },
    ];
    const { result } = renderHook(() => useTimelineDetectors("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    expect(result.current.byMethod.get("value_novelty")?.method).toBe("value_novelty");
  });

  it("writes through the API and updates the cache from the response", async () => {
    const { result } = renderHook(() => useTimelineDetectors("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.canEdit).toBe(true));
    await result.current.set("entropy", {
      params: { fields: ["user"] },
      frame: "self",
      baseline_id: null,
    });
    await waitFor(() =>
      expect(result.current.entries.map((e) => e.method)).toEqual(["entropy"]),
    );
    expect(server.puts).toEqual([
      {
        method: "entropy",
        body: { params: { fields: ["user"] }, frame: "self", baseline_id: null },
      },
    ]);
    await result.current.remove("entropy");
    await waitFor(() => expect(result.current.entries).toEqual([]));
    expect(server.deletes).toEqual(["entropy"]);
  });

  it("derives scope params from an entry", () => {
    const base = { method: "frequency", params: {}, added_by: null, added_at: "" };
    expect(scopeOf({ ...base, frame: "self", baseline_id: null })).toEqual({ frame: "self" });
    expect(scopeOf({ ...base, frame: "baseline", baseline_id: "b1" })).toEqual({
      frame: "baseline",
      baseline_id: "b1",
    });
  });
});

describe("useStreamingSweep", () => {
  beforeEach(() => {
    server.detectors = [];
    asked.calls = [];
    plan.status = "applicable";
  });

  it("issues no findings query when nothing is configured", async () => {
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.planLoading).toBe(false));
    await new Promise((r) => setTimeout(r, 50));
    expect(asked.calls).toEqual([]);
    expect(result.current.total).toBe(0);
  });

  it("runs exactly the configured entries with their own params and scope", async () => {
    server.detectors = [
      { method: "value_novelty", params: { fields: ["user"] }, frame: "self", baseline_id: null },
      {
        method: "frequency",
        params: { series_field: "artifact" },
        frame: "baseline",
        baseline_id: "b1",
      },
    ];
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.done).toBe(2));
    expect(asked.calls).toEqual(
      expect.arrayContaining([
        {
          method: "value_novelty",
          frame: "self",
          baseline_id: undefined,
          params: { fields: ["user"] },
        },
        {
          method: "frequency",
          frame: "baseline",
          baseline_id: "b1",
          params: { series_field: "artifact" },
        },
      ]),
    );
    expect(asked.calls).toHaveLength(2);
    expect(result.current.byMethod.entropy.configured).toBe(false);
    expect(result.current.byMethod.frequency.entry?.baseline_id).toBe("b1");
  });

  it("runs a configured method even when the plan calls it not_applicable", async () => {
    plan.status = "not_applicable";
    server.detectors = [{ method: "charset", params: {}, frame: "self", baseline_id: null }];
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.done).toBe(1));
    expect(asked.calls.map((c) => c.method)).toEqual(["charset"]);
  });
});
