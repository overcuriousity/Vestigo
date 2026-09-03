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
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToolsSheet } from "@/components/analysis/ToolsSheet";
import { METHODS, METHODS_BY_ID } from "@/components/analysis/method-registry";

const sweep = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("@/hooks/useMethodFindings", () => ({
  useStreamingSweep: () => sweep.current,
  useMethodFindings: () => ({ data: undefined, isLoading: false }),
  useIncludeDismissed: () => ({ includeDismissed: false, setIncludeDismissed: () => {} }),
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
vi.mock("@/components/analysis/SimilarEvents", () => ({
  SimilarEvents: () => <div>similar-events</div>,
}));
vi.mock("@/components/analysis/TemplatesView", () => ({
  TemplatesView: () => <div>templates-view</div>,
}));

const baselineStore = vi.hoisted(() => ({ current: { activeBaselineId: null as string | null } }));
vi.mock("@/stores/baseline", () => ({
  useBaselineStore: (sel: (s: unknown) => unknown) => sel(baselineStore.current),
}));
vi.mock("@/api/baselines", () => ({
  baselinesApi: {
    list: () => Promise.resolve({ baselines: [{ id: "bl-1", name: "Feb 24 – Mar 1" }] }),
  },
}));


const capabilities = vi.hoisted(() => ({ current: { embeddings: true, sigma: true } }));
vi.mock("@/api/health", () => ({ useCapabilities: () => capabilities.current }));

const dispositions = vi.hoisted(() => ({
  rows: [] as Record<string, unknown>[],
  removed: [] as string[],
}));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: {
    list: () => Promise.resolve({ dispositions: dispositions.rows }),
    remove: (_c: string, _t: string, id: string) => {
      dispositions.removed.push(id);
      return Promise.resolve({ deleted: true, disposition_id: id });
    },
  },
}));

const readiness = vi.hoisted(() => ({
  current: { stillIngesting: false, nothingToAnalyse: false },
}));
vi.mock("@/hooks/useTimelineReadiness", () => ({
  useTimelineReadiness: () => readiness.current,
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
    pending: false,
    refetch: () => {},
    configured: true,
    entry: { method: id, params: {}, frame: "self", baseline_id: null, added_by: null, added_at: "" },
    ...over,
  };
}

function renderTools(
  props: Record<string, unknown> = {},
  scope: Record<string, unknown> = {},
  overrides: Record<string, unknown> = {},
) {
  sweep.current = {
    scope: { frame: "self", baseline_id: null, baseline_name: null, ...scope },
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
      ...Object.entries(overrides),
    ]),
  };
  return render(
    <ToolsSheet
      caseId="c1"
      timelineId="t1"
      section="methods"
      onRunMethod={() => {}}
      onOpenMethod={() => {}}
      onAddDetector={() => {}}
      onRequestScopeChange={() => {}}
      {...props}
    />,
    { wrapper },
  );
}

describe("ToolsSheet", () => {
  beforeEach(() => {
    capabilities.current = { embeddings: true, sigma: true };
    readiness.current = { stillIngesting: false, nothingToAnalyse: false };
    baselineStore.current = { activeBaselineId: null };
    dispositions.rows = [];
    dispositions.removed = [];
  });

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

  it("retries the query that failed, not an ad hoc run of the same method", () => {
    // Routing Retry through `onRunMethod` ran the method with empty params
    // under the panel scope — a different question than the configured entry's
    // failing one, which could report success while the rail's chip still
    // showed the failure.
    const onRunMethod = vi.fn();
    const refetch = vi.fn();
    renderTools({ onRunMethod }, {}, {
      entropy: state("entropy", { error: true, refetch }),
    });
    within(screen.getByTestId("method-row-entropy"))
      .getByRole("button", { name: /retry/i })
      .click();
    expect(refetch).toHaveBeenCalled();
    expect(onRunMethod).not.toHaveBeenCalled();
  });

  it("lists only configured detectors and offers the wizard for the rest", () => {
    const onAddDetector = vi.fn();
    renderTools({ onAddDetector }, {}, {
      entropy: state("entropy", { configured: false, entry: undefined }),
    });
    expect(screen.getAllByTestId(/^method-row-/)).toHaveLength(METHODS.length - 1);
    expect(screen.queryByTestId("method-row-entropy")).toBeNull();
    screen.getByTestId("tools-add-detector").click();
    expect(onAddDetector).toHaveBeenCalled();
  });

  it("states how many are configured and how many ran", () => {
    renderTools({}, {}, { entropy: state("entropy", { configured: false, entry: undefined }) });
    const summary = screen.getByTestId("methods-summary");
    expect(summary).toHaveTextContent(`${METHODS.length - 1} configured`);
    expect(summary).toHaveTextContent(/ran/);
    expect(summary).not.toHaveTextContent(/skipped/i);
  });

  it("carries the signature and exploration surfaces rather than scattering them", () => {
    // Tabs now, not one scroll — but still one surface. The accounting is
    // scattered if these live on other *pages*, not if they live on other tabs
    // of the same sheet.
    renderTools({ section: "signatures" });
    expect(screen.getByText("sigma-panel")).toBeInTheDocument();
    renderTools({ section: "explore" });
    expect(screen.getByText("patterns-view")).toBeInTheDocument();
  });

  it("opens on the section it was asked for", () => {
    // Every rail affordance into Tools names a section — the scope strip, the
    // skipped-methods summary, the error copy. Landing on the default tab
    // instead would make each of them a click that goes to the wrong place.
    renderTools({ section: "scope" });
    expect(screen.getByTestId("tools-tab-scope")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("scope-switch-self")).toBeInTheDocument();
  });

  it("moves between tabs without a section request", () => {
    renderTools({ section: "methods" });
    fireEvent.click(screen.getByTestId("tools-tab-scope"));
    expect(screen.getByTestId("scope-switch-self")).toBeInTheDocument();
  });

  it("renders no Sigma entry point when Sigma is unconfigured", () => {
    // The house rule for every optional subsystem: absent, not disabled. A
    // disabled control is a question the analyst then has to answer — and a
    // tab that opens onto an explanation of its own absence is the same thing.
    capabilities.current = { embeddings: true, sigma: false };
    renderTools({ section: "signatures" });
    expect(screen.queryByTestId("tools-tab-signatures")).toBeNull();
    expect(screen.queryByText("sigma-panel")).toBeNull();
  });

  it("falls back to a real tab when the requested one does not exist", () => {
    // Sigma unconfigured while something still asks for its section: rendering
    // the selection faithfully would be an empty sheet.
    capabilities.current = { embeddings: true, sigma: false };
    renderTools({ section: "signatures" });
    expect(screen.getByTestId("tools-tab-methods")).toHaveAttribute("aria-selected", "true");
  });

  it("renders no similarity affordance when embeddings are unconfigured", () => {
    capabilities.current = { embeddings: false, sigma: true };
    renderTools({ section: "explore" });
    expect(screen.queryByText(/find events like it/i)).toBeNull();
  });

  it("offers no Sigma scan on a timeline with nothing to scan", () => {
    // Zero matches there reads as "these rules cleared you". They did not —
    // there was nothing to match against.
    readiness.current = { stillIngesting: false, nothingToAnalyse: true };
    renderTools({ section: "signatures" });
    expect(screen.queryByTestId("tools-tab-signatures")).toBeNull();
    expect(screen.queryByText("sigma-panel")).toBeNull();
  });

  it("routes a scope change through a confirm rather than applying it directly", () => {
    const onRequestScopeChange = vi.fn();
    renderTools(
      { onRequestScopeChange, section: "scope" },
      { baseline_id: "bl-1", baseline_name: "Feb 24 – Mar 1" },
    );
    screen.getByTestId("scope-switch-baseline").click();
    expect(onRequestScopeChange).toHaveBeenCalledWith({
      frame: "baseline",
      baselineId: "bl-1",
      baselineName: "Feb 24 – Mar 1",
    });
  });

  it("asks for a definition rather than confirming a baseline frame that cannot take effect", () => {
    // Confirming a baseline frame with no definition would announce a re-run
    // and then silently fall back to self — the store clears the frame with the
    // id. The affordance has to lead to the builder instead.
    const onRequestScopeChange = vi.fn();
    renderTools({ onRequestScopeChange, section: "scope" });
    const button = screen.getByTestId("scope-switch-baseline");
    expect(button).toHaveTextContent(/pick a baseline/i);
    button.click();
    expect(onRequestScopeChange).not.toHaveBeenCalled();
  });

  it("carries the template browser, which is the only surface that can reverse a mute", () => {
    // `events.py` still collapses muted templates out of the grid, histogram
    // and export. Without a surface that lists and unmutes them, a pre-existing
    // mute hides evidence with nothing left to inspect or undo it.
    renderTools({ section: "explore" });
    expect(screen.getByText("templates-view")).toBeInTheDocument();
  });

  it("counts a method still in flight as neither ran nor skipped", () => {
    // On first open the heavy set is queued behind the cheap one. Claiming it
    // "ran" is an accounting that has not happened yet.
    renderTools({}, {}, { value_novelty: state("value_novelty", { pending: true }) });
    expect(screen.getByTestId("methods-summary")).toHaveTextContent(/1 still running/i);
  });

  it("never shows a zero for a method that has not run yet", () => {
    // A queued method has `total === 0` because nothing scanned, and "0" in the
    // found-nothing style is the "checked, clear" misread this surface exists
    // to prevent — the same reason an unscored run renders a dash.
    renderTools({}, {}, { value_novelty: state("value_novelty", { pending: true, total: 0 }) });
    const count = screen.getByTestId("method-count-value_novelty");
    expect(count).not.toHaveTextContent("0");
    expect(count).toHaveTextContent("…");
  });

  it("renders a dash, not a zero, for a method that ran without scoring", () => {
    // A zero asserts the method looked and found nothing, which is exactly
    // what `insufficient_data` says it could not do.
    renderTools(
      {},
      {},
      {
        frequency: state("frequency", {
          dataStatus: "insufficient_data",
          warnings: ["suspect window holds too few buckets to score"],
        }),
      },
    );
    expect(screen.getByTestId("method-count-frequency")).toHaveTextContent("—");
    expect(screen.getByTestId("method-detail-frequency")).toHaveTextContent(/too few buckets/i);
  });

  it("requests the baseline frame when a definition is already selected", () => {
    // `scope.baseline_id` is null in the self frame by construction, so reading
    // `needsDefinition` from it made "Compare baseline" always open the builder
    // and never request the switch it names.
    baselineStore.current = { activeBaselineId: "bl-1" };
    const onRequestScopeChange = vi.fn();
    renderTools({ onRequestScopeChange, section: "scope" });
    screen.getByTestId("scope-switch-baseline").click();
    expect(onRequestScopeChange).toHaveBeenCalledWith(
      expect.objectContaining({ frame: "baseline", baselineId: "bl-1" }),
    );
  });

  /**
   * A `normal` verdict suppresses matching findings in every later sweep, so
   * an analyst who records one by mistake has quietly narrowed what the tool
   * will ever show them. 1.12.0 shipped with the only durable way to undo that
   * unmounted, leaving a four-second toast as the entire revert path.
   */
  describe("recorded verdicts", () => {
    it("lists them under Scope, with a way to take one back", async () => {
      dispositions.rows = [
        {
          id: "d1",
          kind: "normal",
          detector: "value_novelty",
          field: "attr:user_agent",
          value: "curl/7.68.0",
          event_id: null,
          note: null,
        },
      ];
      renderTools({ section: "scope" });
      expect(await screen.findByText(/curl\/7\.68\.0/)).toBeInTheDocument();
      fireEvent.click(await screen.findByTestId("disposition-remove-d1"));
      await vi.waitFor(() => expect(dispositions.removed).toEqual(["d1"]));
    });

    it("says so when there are none, rather than rendering nothing", async () => {
      // An empty region reads as a missing feature; the analyst has to be able
      // to tell "no verdicts recorded" from "this surface is gone again".
      renderTools({ section: "scope" });
      expect(await screen.findByTestId("dispositions-section")).toHaveTextContent(/none yet/i);
    });
  });
});
