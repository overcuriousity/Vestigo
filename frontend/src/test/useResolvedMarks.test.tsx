/**
 * `useResolvedMarks` keys its query on the mark sources alone — switching
 * between two figures that draw marks must not re-resolve them — so a figure
 * that draws none still had the cached answer in `data`, and the caption
 * printed "mark #1 …" under a bar chart with no mark on it.
 */
import { describe, it, expect, vi } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useResolvedMarks } from "@/components/viz/useResolvedMarks";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";

const resolveMarks = vi.fn();
vi.mock("@/api/viz", () => ({ vizApi: { resolveMarks: (...a: unknown[]) => resolveMarks(...a) } }));

const RESOLVED = { marks: [], sources: [{ index: 0, kind: "instant", shown: 1, total: 1 }], cap: 50 };

describe("useResolvedMarks", () => {
  it("exposes the resolution only while the figure draws marks", async () => {
    resolveMarks.mockResolvedValue(RESOLVED);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const marks: ChartConfig["marks"] = [{ kind: "instant", at: "2026-07-20T09:41:00Z", label: "first" }];
    const onTime: ChartConfig = { ...DEFAULT_CHART_CONFIG, chartType: "time", marks };
    const { result, rerender } = renderHook((config: ChartConfig) => useResolvedMarks("c1", "t1", config), {
      wrapper,
      initialProps: onTime,
    });
    await waitFor(() => expect(result.current.data).toEqual(RESOLVED));

    // Same sources, a figure that takes no marks: the cache still holds the
    // answer, the hook must not hand it out.
    rerender({ ...onTime, chartType: "bar", field: "artifact" });
    expect(result.current.data).toBeUndefined();

    // Back on a marked figure the answer is there again.
    rerender(onTime);
    await waitFor(() => expect(result.current.data).toEqual(RESOLVED));
  });
});
