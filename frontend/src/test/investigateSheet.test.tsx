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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InvestigateSheet } from "@/components/analysis/InvestigateSheet";
import { TooltipProvider } from "@/components/ui/Tooltip";

vi.mock("@/hooks/useMethodFindings", () => ({
  useMethodFindings: () => ({ data: undefined, isLoading: false, isError: false }),
  METHOD_LIMIT: 50,
}));

/**
 * The field inventory both field knobs are populated from — one recommended
 * field and one the picker leaves unchecked, which is what lets a test tell an
 * explicit pick apart from the auto default it starts in.
 */
vi.mock("@/api/anomalies", () => ({
  anomaliesApi: {
    fields: async () => ({
      fields: [
        { token: "attr:src_ip", distinct: 42, coverage: 0.98, kind: "categorical", recommended: true },
        { token: "attr:session", distinct: 9001, coverage: 0.4, kind: "identifier", recommended: false },
      ],
    }),
    numericFields: async () => ({ fields: [] }),
  },
}));

// TooltipProvider because the field picker's chips carry their coverage and
// cardinality in a tooltip; AppShell provides it around the real app.
function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

/**
 * A complete finding, not a convenient subset. The sheet now states the
 * verdict in prose built from the payload's own numbers, so a fixture missing
 * `q_value` or `direction` is not "a smaller fixture" — it is a payload the API
 * never sends, and testing against it would prove the sheet works on data that
 * does not exist.
 */
const FINDING = {
  type: "interval_periodicity" as const,
  field: "attr:src_ip",
  value: "10.14.9.203",
  direction: "new_regularity" as const,
  score: 12.71,
  count: 214,
  baseline_count: 3,
  baseline_median_interval: 900,
  window_median_interval: 60,
  baseline_cv: 0.8,
  window_cv: 0.007,
  statistic: 41.2,
  p_value: 1.9e-13,
  q_value: 1.2e-11,
  event_id: "e1",
  event: null,
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

  /**
   * Which fields a method scans is a choice among this timeline's own columns.
   * The rail-plus-overlay refactor deleted the per-detector views and with them
   * both controls that offered those columns, leaving a text box the analyst had
   * to spell `attr:` tokens into from memory. These four hold the fix.
   */
  it("offers the fields knob as a picker, not a text box", async () => {
    renderSheet({ mode: "method", methodId: "value_novelty", onRun: () => {}, query: idleQuery() });
    expect(screen.queryByTestId("method-knob-fields")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /fields/i })).toBeInTheDocument(),
    );
  });

  it("sends the picked fields as a list, not a string the analyst typed", async () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "value_novelty", onRun, query: idleQuery() });
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    // The recommended field starts checked (that is the auto default made
    // visible); adding the unchecked one is the analyst's explicit pick.
    fireEvent.click(await screen.findByText("session"));
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    // An explicit pick travels as a list, which _FieldsParams._join_fields
    // accepts alongside the comma-joined string. An untouched picker sends
    // nothing at all — see the untouched-knob test above.
    expect(onRun).toHaveBeenCalledWith({ fields: ["attr:src_ip", "attr:session"] });
  });

  it("offers a single-field knob as this timeline's own fields", async () => {
    renderSheet({ mode: "method", methodId: "sequence_novelty", onRun: () => {}, query: idleQuery() });
    const select = screen.getByTestId("method-knob-series_field");
    expect(select.tagName).toBe("SELECT");
    // Standard columns immediately; the timeline's attribute keys once the
    // inventory arrives.
    expect(screen.getByRole("option", { name: "Artifact type" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("option", { name: "src_ip" })).toBeInTheDocument());
  });

  it("holds value_combo's picker to the two-to-four fields it can combine", async () => {
    renderSheet({ mode: "method", methodId: "value_combo", onRun: () => {}, query: idleQuery() });
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    expect(await screen.findByText(/Pick 2–4 fields/i)).toBeInTheDocument();
  });

  /**
   * The picker only warns below its floor — refusing the run is this caller's
   * half of that pair. Without it the request 422s and the sheet can only say
   * "this method failed to run", which names neither the cause nor the fix.
   */
  it("refuses to run value_combo below the two fields it can combine", async () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "value_combo", onRun, query: idleQuery() });
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    // Both start checked (auto = top 2); dropping one puts the pick under the floor.
    fireEvent.click(await screen.findByText("src_ip"));
    expect(await screen.findByTestId("method-knob-blocker")).toHaveTextContent(/at least 2 fields/i);
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    expect(onRun).not.toHaveBeenCalled();
  });

  /**
   * An empty pick is a scan over nothing, which returns an empty result set —
   * indistinguishable, once rendered, from "the data is clean".
   */
  it("refuses to run a scan over no fields at all", async () => {
    const onRun = vi.fn();
    renderSheet({ mode: "method", methodId: "value_novelty", onRun, query: idleQuery() });
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    fireEvent.click(await screen.findByText("src_ip"));
    expect(await screen.findByTestId("method-knob-blocker")).toHaveTextContent(
      /at least one field/i,
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    expect(onRun).not.toHaveBeenCalled();
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

describe("the finding sheet's four surfaces", () => {
  it("names the value the finding is about", () => {
    // The verdict sentence says "this value" — the rail row was the only place
    // the value itself appeared, so recognizing it meant closing the sheet.
    renderSheet({ mode: "finding", methodId: "interval_periodicity", finding: FINDING, scope: SCOPE });
    const subject = screen.getByTestId("finding-subject");
    expect(subject).toHaveTextContent("10.14.9.203");
    expect(subject).toHaveTextContent(/src_ip/i);
  });

  it("says what a whole-distribution finding is about instead of naming a value", () => {
    // Nothing isolated one value here, and printing one would misstate what was
    // compared.
    renderSheet({
      mode: "finding",
      methodId: "value_distribution_drift",
      scope: SCOPE,
      finding: {
        type: "value_distribution_drift" as const,
        field: "attr:bytes",
        test: "ks" as const,
        direction: "shifted" as const,
        statistic: 0.31,
        p_value: 2e-9,
        q_value: 4e-8,
        score: 7.4,
        baseline_n: 4200,
        window_n: 3100,
        window_label: "suspect",
        details: {},
      },
    });
    expect(screen.getByTestId("finding-subject")).toHaveTextContent(/bytes/i);
  });

  it("states the finding as a claim, not only as detector shorthand", () => {
    renderSheet({ mode: "finding", methodId: "interval_periodicity", finding: FINDING, scope: SCOPE });
    const verdict = screen.getByTestId("finding-verdict");
    expect(verdict).toHaveTextContent(/more regular than chance allows/i);
    // Built from the payload's own numbers, never rounded to a nicer figure.
    expect(verdict).toHaveTextContent("214 occurrences");
  });

  it("draws the comparison a two-window finding rests on", () => {
    renderSheet({
      mode: "finding",
      methodId: "proportion_shift",
      scope: SCOPE,
      finding: {
        type: "proportion_shift",
        field: "attr:status_code",
        value: "403",
        count: 310,
        baseline_count: 20,
        baseline_rate: 0.02,
        window_rate: 0.31,
        rate_ratio: 15.5,
        direction: "up",
        g_statistic: 184.2,
        p_value: 1e-40,
        q_value: 1.2e-38,
        score: 184.2,
        first_seen: "2026-03-03T21:15:00Z",
        event_id: "e2",
        event: null,
        details: {},
      },
    });
    const evidence = screen.getByTestId("finding-evidence");
    expect(evidence).toHaveTextContent("2.00%");
    expect(evidence).toHaveTextContent("31.00%");
  });

  it("draws nothing for a finding with no second number to compare against", () => {
    // A rare value's rarity IS its score. A chart here would be assembled from
    // numbers nobody measured, which in a forensic tool is worse than no chart.
    renderSheet({
      mode: "finding",
      methodId: "value_novelty",
      scope: SCOPE,
      finding: {
        type: "value_novelty",
        field: "attr:user_agent",
        value: "curl/7.68.0",
        count: 2,
        score: 9.94,
        first_seen: "2026-03-04T02:11:07Z",
        event_id: "e3",
        event: null,
        details: {},
      },
    });
    expect(screen.queryByTestId("finding-evidence")).toBeNull();
    expect(screen.getByTestId("finding-verdict")).toBeInTheDocument();
  });

  it("shows the query shape without claiming it is the executed statement", () => {
    renderSheet({ mode: "finding", methodId: "interval_periodicity", finding: FINDING, scope: SCOPE });
    expect(screen.getByText(/lagInFrame/)).toBeInTheDocument();
    expect(screen.getByText(/not a transcript of the executed query/i)).toBeInTheDocument();
  });

  it("puts the verdict controls on the sheet, where the decision is made", () => {
    renderSheet({ mode: "finding", methodId: "interval_periodicity", finding: FINDING, scope: SCOPE });
    expect(screen.getByTestId("finding-actions")).toBeInTheDocument();
  });

  it("gives the finding-mode knobs a way to submit", () => {
    // They rendered inert: a form of inputs with no submit is a control that
    // lies about being one.
    const onRun = vi.fn();
    renderSheet({
      mode: "finding",
      methodId: "interval_periodicity",
      finding: FINDING,
      scope: SCOPE,
      onRun,
    });
    fireEvent.change(screen.getByTestId("method-knob-fdr_q"), { target: { value: "0.01" } });
    fireEvent.click(screen.getByRole("button", { name: /run with these/i }));
    expect(onRun).toHaveBeenCalledWith({ fdr_q: 0.01 });
  });
});
