/**
 * The sweep's progress line is a claim about coverage, and the rail turns it
 * into an all-clear: when `done === total` and nothing was found, it says "No
 * findings under this scope."
 *
 * Heavy methods are deliberately held back until the cheap set settles, so for
 * a moment they are runnable, enabled: false, and not fetching. Counting that
 * as "settled" made the bar reach 100%, the empty state flash, and the count
 * then fall back — an all-clear asserted over methods that had not started.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { METHODS } from "@/components/analysis/method-registry";

const CHEAP = METHODS.filter((m) => m.costClass === "cheap");
const HEAVY = METHODS.filter((m) => m.costClass === "heavy");

vi.mock("@/hooks/useAnalysisPlan", () => ({
  useAnalysisPlan: () => ({
    planById: Object.fromEntries(
      METHODS.map((m) => [m.id, { method: m.id, status: "applicable", cost_class: m.costClass }]),
    ),
    scope: { frame: "self", baseline_id: null, baseline_name: null },
    isLoading: false,
  }),
  useScopeParams: () => ({ frame: "self", baseline_id: undefined }),
}));

// Cheap methods answer immediately; heavy ones never do, freezing the sweep in
// exactly the window the bug lived in.
vi.mock("@/api/analysis", () => ({
  analysisApi: {
    findings: (_caseId: string, _timelineId: string, { method }: { method: string }) =>
      METHODS.find((m) => m.id === method)?.costClass === "cheap"
        ? Promise.resolve({ results: [], total_findings: 0, scope: {}, cache: "miss" })
        : new Promise(() => {}),
  },
}));

// Nothing runs unprompted: the sweep under test needs every method configured
// on the timeline, in the shape the real `useTimelineDetectors` reads.
vi.mock("@/api/timelines", () => ({
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
}));
vi.mock("@/api/cases", () => ({ casesApi: { get: async () => ({ id: "c1" }) } }));
vi.mock("@/lib/caseAccess", () => ({ canContributeToCase: () => true }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useStreamingSweep progress", () => {
  it("counts every configured method, not only the ones already dispatched", async () => {
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.total).toBe(METHODS.length));
    // And still every one of them while the heavy set has not been dispatched.
    expect(result.current.total).toBe(METHODS.length);
  });

  it("does not report a heavy method as settled while it is still queued", async () => {
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.done).toBe(CHEAP.length));
    // The heavy set is running or waiting to; either way it is not done, so the
    // rail keeps its progress line and makes no claim about coverage.
    expect(result.current.done).toBeLessThan(result.current.total);
    expect(HEAVY.every((m) => result.current.byMethod[m.id].pending)).toBe(true);
  });
});
