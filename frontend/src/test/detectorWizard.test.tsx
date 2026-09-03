/**
 * DetectorWizard — choose, configure, confirm.
 *
 * The gate is advice: a not_applicable card is still selectable. The
 * comparison methods cannot proceed without a baseline. Apply stores exactly
 * the params the knob form reported plus the chosen scope.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DetectorWizard } from "@/components/analysis/DetectorWizard";
import { METHODS } from "@/components/analysis/method-registry";

const detectors = vi.hoisted(() => ({
  entries: [] as Record<string, unknown>[],
  setCalls: [] as unknown[],
}));
vi.mock("@/hooks/useTimelineDetectors", () => ({
  useTimelineDetectors: () => ({
    entries: detectors.entries,
    byMethod: new Map(detectors.entries.map((e) => [e.method, e])),
    set: async (method: string, body: unknown) => {
      detectors.setCalls.push({ method, body });
      return {};
    },
    remove: async () => ({}),
    canEdit: true,
    isSaving: false,
    saveError: null,
  }),
  scopeOf: () => ({ frame: "self" }),
}));

vi.mock("@/hooks/useAnalysisPlan", async () => {
  const { METHODS } = await import("@/components/analysis/method-registry");
  const planById = Object.fromEntries(
    METHODS.map((m) => [
      m.id,
      {
        method: m.id,
        status:
          m.id === "charset"
            ? "not_applicable"
            : m.id === "interval_periodicity"
              ? "needs_setup"
              : "applicable",
        reason: m.id === "charset" ? "every field is enum-like" : "",
        reason_facts: m.id === "charset" ? { fields_above_ceiling: 0 } : {},
        cost_class: m.costClass,
      },
    ]),
  );
  return {
    useAnalysisPlan: () => ({ planById, isLoading: false }),
    useScopeParams: () => ({ frame: "self" }),
  };
});

vi.mock("@/components/analysis/MethodKnobForm", async () => {
  const { useEffect } = await import("react");
  return {
    MethodKnobForm: ({
      onChange,
    }: {
      onChange: (p: Record<string, unknown>, b: string | null) => void;
    }) => {
      // Report once on mount, like the real form's effect does.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      useEffect(() => onChange({ fields: ["user"] }, null), []);
      return <div data-testid="knob-form-stub" />;
    },
  };
});

vi.mock("@/api/baselines", () => ({
  baselinesApi: {
    list: async () => ({ baselines: [{ id: "b1", name: "week before" }] }),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function renderWizard(props: Record<string, unknown> = {}) {
  const onOpenSignatures = vi.fn();
  render(
    <DetectorWizard
      caseId="c1"
      timelineId="t1"
      open
      onOpenChange={() => {}}
      onOpenSignatures={onOpenSignatures}
      {...props}
    />,
    { wrapper },
  );
  return { onOpenSignatures };
}

describe("DetectorWizard", () => {
  beforeEach(() => {
    detectors.entries = [];
    detectors.setCalls = [];
  });

  it("lists every method with its use-when line, its plan verdict and its cost", () => {
    renderWizard();
    for (const m of METHODS) {
      const card = screen.getByTestId(`wizard-card-${m.id}`);
      expect(card).toHaveTextContent(m.useWhen);
      expect(card).toHaveTextContent(m.costClass === "heavy" ? "Full scan" : "Cheap");
    }
    expect(screen.getByTestId("wizard-card-charset")).toHaveTextContent("Cannot apply");
    expect(screen.getByTestId("wizard-card-charset")).toHaveTextContent("fields above ceiling 0");
    expect(screen.getByTestId("wizard-card-interval_periodicity")).toHaveTextContent(
      "Needs a baseline",
    );
  });

  it("still lets a not_applicable method be chosen", () => {
    renderWizard();
    fireEvent.click(screen.getByTestId("wizard-card-charset"));
    expect(screen.getByTestId("wizard-step-configure")).toBeInTheDocument();
  });

  it("routes the Signatures card to the Signatures tab", () => {
    const { onOpenSignatures } = renderWizard();
    fireEvent.click(screen.getByTestId("wizard-card-sigma"));
    expect(onOpenSignatures).toHaveBeenCalled();
  });

  it("walks choose → configure → confirm and stores the entry", async () => {
    renderWizard();
    fireEvent.click(screen.getByTestId("wizard-card-value_novelty"));
    await waitFor(() => expect(screen.getByTestId("wizard-next")).not.toBeDisabled());
    fireEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-summary")).toHaveTextContent(
      "Rare values over user, across the whole timeline.",
    );
    fireEvent.click(screen.getByTestId("wizard-apply"));
    await waitFor(() =>
      expect(detectors.setCalls).toEqual([
        {
          method: "value_novelty",
          body: { params: { fields: ["user"] }, frame: "self", baseline_id: null },
        },
      ]),
    );
  });

  it("requires a baseline for the comparison methods and offers the timeline's definitions", async () => {
    renderWizard();
    fireEvent.click(screen.getByTestId("wizard-card-proportion_shift"));
    expect(screen.getByTestId("wizard-next")).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId("wizard-baseline")).toHaveTextContent("week before"));
    fireEvent.change(screen.getByTestId("wizard-baseline"), { target: { value: "b1" } });
    expect(screen.getByTestId("wizard-next")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-summary")).toHaveTextContent(
      "comparing to baseline “week before”",
    );
  });

  it("opens in edit mode on a configured method", () => {
    detectors.entries = [
      {
        method: "value_novelty",
        params: { fields: ["host"] },
        frame: "self",
        baseline_id: null,
        added_by: null,
        added_at: "",
      },
    ];
    renderWizard({ initialMethod: "value_novelty" });
    expect(screen.getByTestId("wizard-step-configure")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-apply-label")).toHaveTextContent("Save changes");
  });
});
