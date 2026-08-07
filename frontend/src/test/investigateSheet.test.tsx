/**
 * The overlay sheet: where a finding's evidence, the method behind it, its
 * parameters and its query all live.
 *
 * The two structural assertions matter as much as the content ones. It must be
 * absolutely positioned — a flex sibling is what pushed the old panel off
 * screen — and Escape must dismiss it, because the grid behind it is the thing
 * the analyst is trying to get back to.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InvestigateSheet } from "@/components/analysis/InvestigateSheet";

vi.mock("@/hooks/useMethodFindings", () => ({
  useMethodFindings: () => ({ data: undefined, isLoading: false, isError: false }),
  METHOD_LIMIT: 50,
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const FINDING = {
  type: "interval_periodicity" as const,
  field: "attr:src_ip",
  value: "10.14.9.203",
  score: 12.71,
  count: 214,
  event_id: "e1",
  first_seen: "2026-03-04T00:02:11Z",
  details: { period_seconds: 60 },
};

const SCOPE = {
  frame: "baseline" as const,
  baseline_id: "bl-1",
  baseline_name: "Feb 24 – Mar 1",
};

/** A settled, empty findings query — method mode always has one. */
function idleQuery(overrides: Record<string, unknown> = {}) {
  return { data: undefined, isFetching: false, isError: false, ...overrides };
}

function renderSheet(props: Record<string, unknown>) {
  const merged = {
    caseId: "c1",
    timelineId: "t1",
    railWidth: 360,
    onClose: () => {},
    ...props,
  } as React.ComponentProps<typeof InvestigateSheet>;
  return render(<InvestigateSheet {...merged} />, { wrapper });
}

describe("InvestigateSheet", () => {
  it("carries the methodology prose the Method tab used to hold", () => {
    renderSheet({ mode: "method", methodId: "interval_periodicity", onRun: () => {}, query: idleQuery() });
    expect(screen.getByText(/inter-arrival distribution/i)).toBeInTheDocument();
  });

  it("shows a finding's score with its own unit", () => {
    renderSheet({
      mode: "finding",
      methodId: "interval_periodicity",
      finding: FINDING,
      scope: SCOPE,
    });
    expect(screen.getByTestId("finding-score")).toHaveTextContent("12.71");
    expect(screen.getByTestId("finding-score")).toHaveTextContent("−log₁₀ p");
  });

  it("states the scope the finding was computed under", () => {
    renderSheet({
      mode: "finding",
      methodId: "interval_periodicity",
      finding: FINDING,
      scope: SCOPE,
    });
    expect(screen.getByTestId("finding-scope")).toHaveTextContent("Feb 24 – Mar 1");
  });

  it("says plainly when a finding was computed without a baseline", () => {
    renderSheet({
      mode: "finding",
      methodId: "interval_periodicity",
      finding: FINDING,
      scope: { frame: "self", baseline_id: null, baseline_name: null },
    });
    expect(screen.getByTestId("finding-scope")).toHaveTextContent(/no baseline/i);
  });

  it("names the kind of claim a finding is making", () => {
    renderSheet({
      mode: "finding",
      methodId: "interval_periodicity",
      finding: FINDING,
      scope: SCOPE,
    });
    expect(screen.getByTestId("finding-class")).toHaveTextContent(/odd, not necessarily bad/i);
  });

  it("renders one knob per declared parameter", () => {
    renderSheet({ mode: "method", methodId: "sequence_novelty", onRun: () => {}, query: idleQuery() });
    expect(screen.getAllByTestId("method-knob")).toHaveLength(3);
  });

  it("renders as an overlay, never as a flex sibling that could widen the row", () => {
    renderSheet({ mode: "method", methodId: "value_novelty", onRun: () => {}, query: idleQuery() });
    const sheet = screen.getByTestId("investigate-sheet");
    expect(sheet.className).toContain("absolute");
    expect(sheet.className).not.toContain("shrink-0");
    expect(screen.getByTestId("investigate-scrim")).toBeInTheDocument();
  });

  it("closes on the escape key so the grid is one keystroke away", () => {
    const onClose = vi.fn();
    renderSheet({ mode: "method", methodId: "value_novelty", onClose, onRun: () => {}, query: idleQuery() });
    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("sends nothing for an untouched knob", () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "charset", onRun, query: idleQuery() });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    expect(onRun).toHaveBeenCalledWith({});
  });

  it("sends group_field when the analyst types one", () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "charset", onRun, query: idleQuery() });
    fireEvent.change(screen.getByTestId("method-knob-group_field"), {
      target: { value: "display_name" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    expect(onRun).toHaveBeenCalledWith({ group_field: "display_name" });
  });

  it("sends a numeric knob as a number, not the analyst's typing", () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "sequence_novelty", onRun, query: idleQuery() });
    fireEvent.change(screen.getByTestId("method-knob-max_gap_seconds"), {
      target: { value: "300" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    expect(onRun).toHaveBeenCalledWith({ max_gap_seconds: 300 });
  });

  it("says a run found nothing rather than showing nothing at all", () => {
    renderSheet({
      mode: "method",
      methodId: "charset",
      onRun: () => {},
      query: idleQuery({ data: { results: [], total_findings: 0 } }),
    });
    expect(screen.getByText(/found nothing under these parameters/i)).toBeInTheDocument();
  });

  it("shows a gated method's results in the sheet — the gate is advice, not a lock", () => {
    renderSheet({
      mode: "method",
      methodId: "value_novelty",
      onRun: () => {},
      query: idleQuery({
        data: {
          results: [
            {
              type: "value_novelty",
              field: "attr:user_agent",
              value: "curl/7.68.0",
              score: 8.2,
              count: 1,
              event_id: "e9",
              details: {},
            },
          ],
          total_findings: 1,
        },
      }),
    });
    expect(screen.getByText(/curl\/7\.68\.0/)).toBeInTheDocument();
  });
});
