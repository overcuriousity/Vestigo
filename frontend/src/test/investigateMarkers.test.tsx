/**
 * The rail publishes its timestamped findings into ExplorerPage state, which
 * makes marker publication a feedback edge: parent state -> re-render -> the
 * rail recomputes markers -> it publishes again. If the published value changes
 * identity for any reason other than a real change, that edge never settles and
 * the Explorer freezes.
 *
 * These tests run the *real* hooks (only the HTTP layer is stubbed), because
 * the bug lived entirely in the identity of what those hooks return — a suite
 * that mocks `useStreamingSweep` cannot see it by construction.
 */
import type { ReactNode } from "react";
import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { InvestigateRail } from "@/components/analysis/InvestigateRail";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { METHODS } from "@/components/analysis/method-registry";
import type { AnomalyMarker } from "@/api/types";

vi.mock("@/hooks/useTimelineReadiness", () => ({
  useTimelineReadiness: () => ({ stillIngesting: false, nothingToAnalyse: false }),
}));
vi.mock("@/hooks/useSigmaFindings", () => ({
  useSigmaFindings: () => ({ findings: [], isLoading: false, available: false }),
}));

// Nothing runs unprompted, so the sweep under test needs every method
// configured on the timeline — the shape the real hooks read.
vi.mock("@/api/timelines", async () => {
  const { METHODS } = await import("@/components/analysis/method-registry");
  return {
    timelinesApi: {
      get: async () => ({
        id: "t1",
        case_id: "c1",
        detectors: METHODS.map((m) => ({
          method: m.id,
          params: {},
          frame: "self",
          baseline_id: null,
          added_by: null,
          added_at: "",
        })),
      }),
    },
  };
});
vi.mock("@/api/cases", () => ({ casesApi: { get: async () => ({ id: "c1" }) } }));
vi.mock("@/lib/caseAccess", () => ({ canContributeToCase: () => true }));
vi.mock("@/api/baselines", () => ({ baselinesApi: { list: async () => ({ baselines: [] }) } }));

/** One timestamped finding for a method that maps to a marker. */
const NOVELTY = {
  type: "value_novelty",
  field: "attr:user_agent",
  value: "curl/7.68.0",
  score: 9.94,
  count: 2,
  event_id: "e1",
  first_seen: "2026-03-04T02:11:07Z",
  details: {},
};

vi.mock("@/api/analysis", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/analysis")>();
  return {
    ...actual,
    analysisApi: {
      plan: () =>
        Promise.resolve({
          methods: METHODS.map((m) => ({
            method: m.id,
            status: "applicable",
            reason: "",
            reason_facts: {},
            cost_class: m.costClass,
          })),
          scope: { frame: "self", baseline_id: null, baseline_name: null },
          events_total: 100,
        }),
      findings: (_c: string, _t: string, { method }: { method: string }) =>
        Promise.resolve({
          results: method === "value_novelty" ? [NOVELTY] : [],
          total_findings: method === "value_novelty" ? 1 : 0,
          scope: { frame: "self", baseline_id: null, baseline_name: null },
          cache: "miss",
        }),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("marker publication", () => {
  it("keeps byMethod referentially stable across renders that changed nothing", async () => {
    const { result, rerender } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    // Both halves matter: while the plan is still loading nothing is runnable,
    // so `done === total` is vacuously true at zero.
    await waitFor(() => {
      expect(result.current.total).toBe(METHODS.length);
      expect(result.current.done).toBe(result.current.total);
    });
    const before = result.current.byMethod;
    rerender();
    rerender();
    expect(result.current.byMethod).toBe(before);
  });

  it("settles instead of looping when the parent stores the markers in state", async () => {
    let renders = 0;
    function Host() {
      const [markers, setMarkers] = useState<AnomalyMarker[]>([]);
      renders += 1;
      return (
        <>
          <span data-testid="marker-count">{markers.length}</span>
          <InvestigateRail
            caseId="c1"
            timelineId="t1"
            onSelectFinding={() => {}}
            onOpenTools={() => {}}
            onAddDetector={() => {}}
            onSelectEvent={() => {}}
            onAnomalyMarkers={setMarkers}
          />
        </>
      );
    }

    const { findByTestId } = render(<Host />, { wrapper });
    // The finding does reach the histogram...
    await waitFor(async () =>
      expect((await findByTestId("marker-count")).textContent).toBe("1"),
    );
    const settled = renders;
    await new Promise((r) => setTimeout(r, 100));
    // ...and publishing it does not keep re-entering. A handful of renders may
    // still land from queries settling; the bug produced hundreds per 100ms.
    expect(renders - settled).toBeLessThan(5);
  });
});
