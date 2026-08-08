/**
 * useScopeChange applies a confirmed scope change to the baseline store.
 *
 * The store couples the two fields — setting a definition implies the baseline
 * frame — so the hook has to be careful about which setter it reaches for. The
 * bug this covers: switching to `self` also cleared the chosen definition, so a
 * baseline → self → baseline round-trip landed on "Pick a baseline…" and the
 * builder rather than returning to the comparison the analyst was reading.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useScopeChange } from "@/hooks/useScopeChange";
import { useBaselineStore } from "@/stores/baseline";

vi.mock("@/hooks/useAnalysisPlan", () => ({
  useAnalysisPlan: () => ({
    planById: {},
    scope: { frame: "self", baseline_id: null, baseline_name: null },
  }),
}));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: { list: () => Promise.resolve({ dispositions: [] }) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useScopeChange.confirm", () => {
  beforeEach(() => {
    useBaselineStore.setState({ frame: "self", activeBaselineId: null });
  });

  it("keeps the chosen definition when switching to the self frame", () => {
    const { result } = renderHook(() => useScopeChange("c1", "t1"), { wrapper });

    act(() => result.current.request({ frame: "baseline", baselineId: "bl-1" }));
    act(() => result.current.confirm());
    expect(useBaselineStore.getState()).toMatchObject({
      frame: "baseline",
      activeBaselineId: "bl-1",
    });

    act(() => result.current.request({ frame: "self" }));
    act(() => result.current.confirm());
    // Frame moved; the selection did not. `useScopeParams` reads the frame
    // first, so nothing runs against the retained id while in `self`.
    expect(useBaselineStore.getState()).toMatchObject({
      frame: "self",
      activeBaselineId: "bl-1",
    });

    // ...which is what makes going back a switch rather than a re-pick.
    act(() => result.current.request({ frame: "baseline", baselineId: "bl-1" }));
    act(() => result.current.confirm());
    expect(useBaselineStore.getState().frame).toBe("baseline");
  });

  it("refuses a baseline frame with no definition", () => {
    const { result } = renderHook(() => useScopeChange("c1", "t1"), { wrapper });
    act(() => result.current.request({ frame: "baseline" }));
    expect(result.current.pending).toBeNull();
  });
});
