/**
 * The Tools sheet is the machinery accounting: what was examined, what was
 * not, under what scope, with what result.
 *
 * That question has evidentiary weight, so the assertions here are mostly
 * about honesty rather than layout — a skip must show its arithmetic, a
 * skipped method must stay runnable, and a method that needs an analyst action
 * must offer that action rather than a "run anyway" that cannot help.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToolsSheet } from "@/components/analysis/ToolsSheet";
import { METHODS, METHODS_BY_ID } from "@/components/analysis/method-registry";

const sweep = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("@/hooks/useMethodFindings", () => ({
  useStreamingSweep: () => sweep.current,
  useMethodFindings: () => ({ data: undefined, isLoading: false }),
  METHOD_LIMIT: 50,
}));
vi.mock("@/components/analysis/SigmaPanel", () => ({
  SigmaPanel: () => <div>sigma-panel</div>,
}));
vi.mock("@/components/analysis/PatternsView", () => ({
  PatternsView: () => <div>patterns-view</div>,
}));
vi.mock("@/components/analysis/BaselineBuilderDrawer", () => ({
  BaselineBuilderDrawer: () => null,
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function state(id: string, over: Record<string, unknown> = {}) {
  return {
    meta: METHODS_BY_ID[id as keyof typeof METHODS_BY_ID],
    plan: { method: id, status: "applicable", reason: "", reason_facts: {}, cost_class: "cheap" },
    status: "applicable",
    findings: [],
    total: 0,
    isLoading: false,
    error: false,
    ...over,
  };
}

function renderTools(props: Record<string, unknown> = {}) {
  sweep.current = {
    scope: { frame: "self", baseline_id: null, baseline_name: null },
    done: 10,
    total: 10,
    planLoading: false,
    byMethod: Object.fromEntries([
      ...METHODS.map((m) => [m.id, state(m.id)]),
      [
        "numeric_range",
        state("numeric_range", {
          status: "not_applicable",
          total: 0,
          plan: {
            method: "numeric_range",
            status: "not_applicable",
            reason: "no field parses as numeric",
            reason_facts: { numeric_fields: 0, sampled: 19 },
            cost_class: "heavy",
          },
        }),
      ],
      [
        "proportion_shift",
        state("proportion_shift", {
          status: "needs_setup",
          plan: {
            method: "proportion_shift",
            status: "needs_setup",
            reason: "needs a baseline window to compare against",
            reason_facts: {},
            cost_class: "heavy",
          },
        }),
      ],
      ["value_novelty", state("value_novelty", { total: 12 })],
    ]),
  };
  return render(
    <ToolsSheet
      caseId="c1"
      timelineId="t1"
      section="methods"
      onRunMethod={() => {}}
      onOpenMethod={() => {}}
      onRequestScopeChange={() => {}}
      {...props}
    />,
    { wrapper },
  );
}

describe("ToolsSheet", () => {
  it("shows the arithmetic behind a skip, not a bare verdict", () => {
    renderTools();
    const row = screen.getByTestId("method-row-numeric_range");
    expect(row).toHaveTextContent("no field parses as numeric");
    expect(row).toHaveTextContent("0");
    expect(row).toHaveTextContent("19");
  });

  it("offers Run anyway on a method the gate skipped", () => {
    renderTools();
    expect(
      within(screen.getByTestId("method-row-numeric_range")).getByRole("button", {
        name: /run anyway/i,
      }),
    ).toBeEnabled();
  });

  it("offers the setup action rather than Run anyway when the method needs setup", () => {
    renderTools();
    const row = screen.getByTestId("method-row-proportion_shift");
    expect(within(row).queryByRole("button", { name: /run anyway/i })).toBeNull();
    expect(within(row).getByRole("button", { name: /baseline/i })).toBeInTheDocument();
  });

  it("runs a skipped method when asked", () => {
    const onRunMethod = vi.fn();
    renderTools({ onRunMethod });
    within(screen.getByTestId("method-row-numeric_range"))
      .getByRole("button", { name: /run anyway/i })
      .click();
    expect(onRunMethod).toHaveBeenCalledWith("numeric_range");
  });

  it("lists every method, ran and skipped, in one accounting", () => {
    renderTools();
    expect(screen.getAllByTestId(/^method-row-/)).toHaveLength(METHODS.length);
  });

  it("states how many were considered, ran and skipped", () => {
    renderTools();
    const summary = screen.getByTestId("methods-summary");
    expect(summary).toHaveTextContent(String(METHODS.length));
    expect(summary).toHaveTextContent(/skipped/i);
  });

  it("carries the signature and exploration surfaces rather than scattering them", () => {
    renderTools();
    expect(screen.getByText("sigma-panel")).toBeInTheDocument();
    expect(screen.getByText("patterns-view")).toBeInTheDocument();
  });

  it("routes a scope change through a confirm rather than applying it directly", () => {
    const onRequestScopeChange = vi.fn();
    renderTools({ onRequestScopeChange });
    screen.getByTestId("scope-switch-baseline").click();
    expect(onRequestScopeChange).toHaveBeenCalled();
  });
});
