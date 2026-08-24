/**
 * Changing scope invalidates every cached method at once and reframes every
 * verdict already recorded, so it is a decision, not a toggle. The dialog has
 * to name that consequence before anything moves, and has to be clear that
 * existing verdicts survive it.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ScopeChangeDialog } from "@/components/analysis/ScopeChangeDialog";

const CURRENT = {
  frame: "baseline" as const,
  baseline_id: "bl-1",
  baseline_name: "Feb 24 – Mar 1",
};
const NEXT = { frame: "self" as const, baselineId: undefined };

function renderDialog(props: Record<string, unknown> = {}) {
  return render(
    <ScopeChangeDialog
      open
      current={CURRENT}
      next={NEXT}
      methodsToRerun={9}
      affectedVerdicts={4}
      onConfirm={() => {}}
      onCancel={() => {}}
      {...props}
    />,
  );
}

describe("ScopeChangeDialog", () => {
  it("names the consequence before anything changes", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("9");
    expect(dialog).toHaveTextContent("4");
  });

  it("does not change scope until confirmed", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    renderDialog({ onConfirm, onCancel });
    screen.getByRole("button", { name: /cancel/i }).click();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).toHaveBeenCalled();
  });

  it("applies the change when confirmed", () => {
    const onConfirm = vi.fn();
    renderDialog({ onConfirm });
    screen.getByRole("button", { name: /change scope/i }).click();
    expect(onConfirm).toHaveBeenCalled();
  });

  it("says verdicts are kept, not discarded", () => {
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveTextContent(/kept/i);
  });

  it("names both the outgoing and incoming scope", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Feb 24 – Mar 1");
    expect(dialog).toHaveTextContent(/all events/i);
  });

  it("does not mention verdicts when none were recorded under the old scope", () => {
    renderDialog({ affectedVerdicts: 0 });
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/verdicts/i);
  });
});

describe("useDisposition scope stamping", () => {
  it("sends the active scope with every verdict", async () => {
    vi.resetModules();
    const create = vi.fn().mockResolvedValue({ disposition: { id: "d1" } });
    vi.doMock("@/api/dispositions", () => ({ dispositionsApi: { create, remove: vi.fn() } }));
    vi.doMock("@/api/anomalies", () => ({
      anomaliesApi: { persistFinding: vi.fn().mockResolvedValue({}) },
    }));
    vi.doMock("@/stores/toasts", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
    vi.doMock("@/stores/baseline", () => ({
      useBaselineStore: (sel: (s: unknown) => unknown) =>
        sel({ frame: "baseline", activeBaselineId: "bl-1" }),
    }));

    const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
    const { renderHook, waitFor } = await import("@testing-library/react");
    const { useDisposition } = await import("@/hooks/useDisposition");

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useDisposition("c1", "t1"), {
      wrapper: ({ children }: { children: React.ReactNode }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });

    result.current.mutate({
      kind: "normal",
      detector: "value_novelty",
      field: "attr:user_agent",
      value: "curl/7.68.0",
    });

    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0][2].analysis_scope).toEqual({
      frame: "baseline",
      baseline_id: "bl-1",
    });
  });
});
