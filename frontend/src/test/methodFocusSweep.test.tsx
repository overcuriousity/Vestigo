import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const findingsMock = vi.fn();
const planMock = vi.fn();

vi.mock("@/api/analysis", () => ({
  analysisApi: {
    plan: (...a: unknown[]) => planMock(...a),
    findings: (...a: unknown[]) => findingsMock(...a),
  },
}));

import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { useAuthStore } from "@/stores/auth";
import { METHODS } from "@/components/analysis/method-registry";
import type { User } from "@/api/types";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  findingsMock.mockReset();
  planMock.mockReset();
  findingsMock.mockResolvedValue({ method: "x", results: [], warnings: [] });
  planMock.mockResolvedValue({
    scope: {},
    methods: METHODS.map((m) => ({ method: m.id, status: "applicable" })),
  });
  useAuthStore.getState().setUser({
    id: "u1",
    username: "analyst",
    is_admin: false,
    preferences: {
      analysis_method_focus: { "tl-1": { proportion_shift: ["attr:http_uri"] } },
    },
  } as unknown as User);
});

/**
 * #341: the focus has to reach the sweep, not just the one-off run in the
 * Tools sheet — the complaint was that closing the sheet put every field back.
 * It narrows what is actually scanned, by sending an explicit `fields`, which
 * bypasses the shared `field_overrides` layer by contract.
 */
describe("a focused method in the sweep", () => {
  it("sends the focused fields for that method only", async () => {
    renderHook(() => useStreamingSweep("c1", "tl-1"), { wrapper });

    await waitFor(() => expect(findingsMock).toHaveBeenCalled());

    const callFor = (method: string) =>
      findingsMock.mock.calls.find((c) => c[2]?.method === method)?.[2];

    await waitFor(() => expect(callFor("proportion_shift")).toBeTruthy());
    expect(callFor("proportion_shift").params).toEqual({ fields: ["attr:http_uri"] });

    const unfocused = findingsMock.mock.calls
      .map((c) => c[2])
      .filter((o) => o.method !== "proportion_shift");
    expect(unfocused.length).toBeGreaterThan(0);
    for (const opts of unfocused) {
      expect(opts.params?.fields).toBeUndefined();
    }
  });

  it("sends no fields at all when nothing is focused", async () => {
    useAuthStore.getState().setUser({
      id: "u1",
      username: "analyst",
      is_admin: false,
      preferences: {},
    } as unknown as User);

    renderHook(() => useStreamingSweep("c1", "tl-1"), { wrapper });

    await waitFor(() => expect(findingsMock).toHaveBeenCalled());
    for (const call of findingsMock.mock.calls) {
      expect(call[2].params?.fields).toBeUndefined();
    }
  });

  it("does not apply another timeline's focus", async () => {
    renderHook(() => useStreamingSweep("c1", "tl-other"), { wrapper });

    await waitFor(() => expect(findingsMock).toHaveBeenCalled());
    for (const call of findingsMock.mock.calls) {
      expect(call[2].params?.fields).toBeUndefined();
    }
  });
});

describe("the focus disclosure strip", () => {
  it("names the focused method, its fields, and offers a way out", async () => {
    const { MethodFocusStrip } = await import("@/components/analysis/MethodFocusStrip");
    const { render, screen } = await import("@testing-library/react");
    const onClear = vi.fn();

    render(
      <MethodFocusStrip
        focus={{ proportion_shift: ["attr:http_uri"] }}
        onClear={onClear}
      />,
    );

    const strip = screen.getByTestId("method-focus-strip");
    expect(strip.textContent).toContain("attr:http_uri");
    expect(strip.textContent).toContain("Only you see this");
    // "nothing is reported about the rest" — the held-back half must be stated,
    // not merely implied by the narrowing.
    expect(strip.textContent).toMatch(/nothing is reported about the rest/i);

    screen.getByRole("button", { name: /Clear focus on/ }).click();
    expect(onClear).toHaveBeenCalledWith("proportion_shift");
  });

  it("renders nothing when no method is focused", async () => {
    const { MethodFocusStrip } = await import("@/components/analysis/MethodFocusStrip");
    const { render, screen } = await import("@testing-library/react");

    render(<MethodFocusStrip focus={{}} onClear={vi.fn()} />);

    expect(screen.queryByTestId("method-focus-strip")).toBeNull();
  });
});
