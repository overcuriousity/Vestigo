/**
 * Muting a detector.
 *
 * A capture with disordered clocks makes `timestamp_order` fire on millions of
 * rows: every finding true, none of them the investigation. The mute exists so
 * an analyst can stop reading past that, which puts it one step away from the
 * thing this whole surface is built to prevent — a quiet list that reads as
 * "clear" when it is really "not asked".
 *
 * So the assertions here are about the two halves of that bargain: the mute has
 * to actually reach every surface (no findings, no histogram marks, no query at
 * all), and it has to be impossible to miss (a count in the rail, a labeled row
 * in Tools, still runnable on request).
 *
 * These run the *real* hooks with only the HTTP layer stubbed, because "the
 * query is never issued" is a claim about the hooks and a suite that mocks
 * `useStreamingSweep` cannot see it by construction.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InvestigateRail } from "@/components/analysis/InvestigateRail";
import { METHODS } from "@/components/analysis/method-registry";
import type { AnomalyMarker } from "@/api/types";

vi.mock("@/hooks/useTimelineReadiness", () => ({
  useTimelineReadiness: () => ({ stillIngesting: false, nothingToAnalyse: false }),
}));
vi.mock("@/hooks/useSigmaFindings", () => ({
  useSigmaFindings: () => ({ findings: [] }),
}));

/**
 * One timestamped finding each for the two methods these tests care about —
 * the muted one and a control that must keep working around it. Every other
 * method returns nothing, so the assertions are about these two.
 */
const FINDINGS: Record<string, Record<string, unknown>> = {
  value_novelty: {
    type: "value_novelty",
    field: "attr:user_agent",
    value: "curl/7.68.0",
    score: 9.94,
    count: 2,
    event_id: "e1",
    first_seen: "2026-03-04T02:11:07Z",
    details: {},
  },
  timestamp_order: {
    type: "timestamp_order",
    source_id: "src-0123456789abcdef0123456789abcdef",
    event_id: "e2",
    line_number: 4711,
    skew_seconds: 92.5,
    timestamp: "2026-03-04T02:12:00Z",
    score: 92.5,
    details: {},
  },
};

const asked = vi.hoisted(() => ({ methods: [] as string[] }));
const timeline = vi.hoisted(() => ({ muted: [] as string[] }));
const patched = vi.hoisted(() => ({ calls: [] as string[][] }));

vi.mock("@/api/analysis", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/analysis")>();
  return {
    ...actual,
    analysisApi: {
      plan: () =>
        Promise.resolve({
          // Every method applicable, deliberately: the plan must not know
          // about mutes, so a muted method still reports it *could* run.
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
      findings: (_c: string, _t: string, { method }: { method: string }) => {
        asked.methods.push(method);
        const hit = FINDINGS[method];
        return Promise.resolve({
          results: hit ? [hit] : [],
          total_findings: hit ? 1 : 0,
          scope: { frame: "self", baseline_id: null, baseline_name: null },
          cache: "miss",
        });
      },
    },
  };
});

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: () =>
      Promise.resolve({ id: "t1", case_id: "c1", muted_methods: [...timeline.muted] }),
    patchMutedMethods: (_c: string, _t: string, next: string[]) => {
      patched.calls.push(next);
      timeline.muted = next;
      return Promise.resolve({ id: "t1", case_id: "c1", muted_methods: next });
    },
  },
}));

vi.mock("@/api/cases", () => ({
  casesApi: { get: () => Promise.resolve({ id: "c1", access_level: "contribute" }) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function renderRail(onAnomalyMarkers?: (m: AnomalyMarker[]) => void) {
  return render(
    <InvestigateRail
      caseId="c1"
      timelineId="t1"
      onSelectFinding={() => {}}
      onOpenTools={() => {}}
      onSelectEvent={() => {}}
      onAnomalyMarkers={onAnomalyMarkers}
    />,
    { wrapper },
  );
}

describe("muting a detector", () => {
  beforeEach(() => {
    asked.methods = [];
    patched.calls = [];
    timeline.muted = [];
  });

  it("never runs the muted method at all", async () => {
    timeline.muted = ["timestamp_order"];
    renderRail();
    // Wait for the sweep to have issued the rest, so "absent" is a real
    // absence rather than a race with a request still being scheduled.
    await waitFor(() => expect(asked.methods).toContain("value_novelty"));
    await waitFor(() => expect(asked.methods.length).toBe(METHODS.length - 1));
    expect(asked.methods).not.toContain("timestamp_order");
  });

  it("keeps the muted method's findings off the histogram", async () => {
    // The rail is the only publisher of anomaly markers, so a mute that stops
    // at the findings list would leave its marks on the timeline with no row
    // left to explain them.
    timeline.muted = ["timestamp_order"];
    const markers: AnomalyMarker[][] = [];
    renderRail((m) => markers.push(m));
    await waitFor(() => expect(asked.methods.length).toBe(METHODS.length - 1));
    await waitFor(() => expect(markers.at(-1)?.length).toBeGreaterThan(0));
    expect(markers.at(-1)?.some((m) => m.detector === "timestamp_order")).toBe(false);
  });

  it("says how many detectors it is holding back", async () => {
    // The whole safety argument. Without this the rail is quietly shorter and
    // nothing on screen says why.
    timeline.muted = ["timestamp_order", "entropy"];
    renderRail();
    await waitFor(() =>
      expect(screen.getByTestId("detector-mute-count")).toHaveTextContent("2 muted"),
    );
  });

  it("claims nothing when nothing is muted", async () => {
    renderRail();
    await waitFor(() => expect(screen.getByTestId("detector-mute-strip")).toBeInTheDocument());
    expect(screen.queryByTestId("detector-mute-count")).toBeNull();
  });

  it("mutes a detector by its method id", async () => {
    renderRail();
    fireEvent.click(screen.getByTestId("detector-mute-toggle"));
    await waitFor(() => expect(screen.getByTestId("mute-chip-timestamp_order")).toBeEnabled());
    fireEvent.click(screen.getByTestId("mute-chip-timestamp_order"));
    await waitFor(() => expect(patched.calls).toEqual([["timestamp_order"]]));
  });

  it("offers a way back out of every mute at once", async () => {
    timeline.muted = ["timestamp_order", "entropy"];
    renderRail();
    fireEvent.click(screen.getByTestId("detector-mute-toggle"));
    await waitFor(() => expect(screen.getByTestId("unmute-all")).toBeEnabled());
    fireEvent.click(screen.getByTestId("unmute-all"));
    await waitFor(() => expect(patched.calls).toEqual([[]]));
  });

  it("does not report an empty feed as clear when everything is muted", async () => {
    // "No method applies to this data yet" blames the gate for a choice an
    // analyst made. Two different situations, two different states.
    for (const m of METHODS) timeline.muted.push(m.id);
    renderRail();
    await waitFor(() =>
      expect(screen.getByText(/every detector for this view is muted/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/no method applies to this data/i)).toBeNull();
  });
});
