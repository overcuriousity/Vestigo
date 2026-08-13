/**
 * The rail's contract: group findings by what kind of claim they are, never
 * present a gated-off method as an all-clear, and say what scope you are
 * looking at.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InvestigateRail } from "@/components/analysis/InvestigateRail";
import { METHODS_BY_ID } from "@/components/analysis/method-registry";

const sweep = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
const dismissed = vi.hoisted(() => ({
  current: { includeDismissed: false, setIncludeDismissed: (_: boolean) => {} },
}));
vi.mock("@/hooks/useMethodFindings", () => ({
  useStreamingSweep: () => sweep.current,
  useMethodFindings: () => ({ data: undefined, isLoading: false }),
  useIncludeDismissed: () => dismissed.current,
  METHOD_LIMIT: 50,
}));

const readiness = vi.hoisted(() => ({
  current: { stillIngesting: false, nothingToAnalyse: false },
}));
vi.mock("@/hooks/useTimelineReadiness", () => ({
  useTimelineReadiness: () => readiness.current,
}));

const sigma = vi.hoisted(() => ({ current: [] as unknown[] }));
vi.mock("@/hooks/useSigmaFindings", () => ({
  useSigmaFindings: () => ({ findings: sigma.current, isLoading: false, available: true }),
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const finding = (over: Record<string, unknown> = {}) => ({
  type: "value_novelty",
  field: "attr:user_agent",
  value: "curl/7.68.0",
  score: 9.94,
  count: 2,
  event_id: "e1",
  first_seen: "2026-03-04T02:11:07Z",
  details: {},
  ...over,
});

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

function renderRail(
  overrides: Record<string, unknown>,
  ready = readiness.current,
  props: Record<string, unknown> = {},
) {
  readiness.current = ready;
  sweep.current = {
    scope: { frame: "self", baseline_id: null, baseline_name: null },
    done: 2,
    total: 2,
    planLoading: false,
    byMethod: {},
    ...overrides,
  };
  return render(
    <InvestigateRail
      caseId="c1"
      timelineId="t1"
      onSelectFinding={() => {}}
      onOpenTools={() => {}}
      onSelectEvent={() => {}}
      {...props}
    />,
    { wrapper },
  );
}

describe("InvestigateRail", () => {
  beforeEach(() => {
    readiness.current = { stillIngesting: false, nothingToAnalyse: false };
    dismissed.current = { includeDismissed: false, setIncludeDismissed: () => {} };
  });

  it("orders groups strongest-claim first", () => {
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", { findings: [finding()], total: 1 }),
        log_template: state("log_template", {
          findings: [{ template: "GET <PATH> <NUM>", count: 6, template_hash: "h1" }],
          total: 1,
        }),
      },
    });
    const headings = screen.getAllByTestId("evidence-group").map((el) => el.textContent);
    expect(headings[0]).toContain("Statistical outliers");
    expect(headings[1]).toContain("Exploration");
  });

  it("states the scope it is showing findings under", () => {
    renderRail({
      scope: { frame: "baseline", baseline_id: "bl-1", baseline_name: "Feb 24 – Mar 1" },
    });
    expect(screen.getByTestId("scope-strip")).toHaveTextContent("Feb 24 – Mar 1");
  });

  it("never renders a gated method as an all-clear", () => {
    renderRail({
      byMethod: {
        numeric_range: state("numeric_range", {
          status: "not_applicable",
          plan: {
            method: "numeric_range",
            status: "not_applicable",
            reason: "no field parses as numeric",
            reason_facts: { numeric_fields: 0, sampled: 19 },
            cost_class: "heavy",
          },
        }),
      },
    });
    expect(screen.queryByText(/0 findings/i)).toBeNull();
    expect(screen.getByTestId("skipped-summary")).toHaveTextContent("1");
  });

  it("shows progress while methods are still settling", () => {
    renderRail({ done: 3, total: 9 });
    expect(screen.getByTestId("sweep-progress")).toHaveTextContent("3");
    expect(screen.getByTestId("sweep-progress")).toHaveTextContent("9");
  });

  it("hides the progress line once everything has settled", () => {
    renderRail({ done: 9, total: 9 });
    expect(screen.queryByTestId("sweep-progress")).toBeNull();
  });

  it("selects a finding by its API method id, not the legacy UI slug", async () => {
    // The two registries disagree: detector-registry keys on "novelty", the
    // method registry on "value_novelty". Handing the sheet the slug would
    // resolve to nothing.
    const onSelectFinding = vi.fn();
    sweep.current = {
      scope: { frame: "self", baseline_id: null, baseline_name: null },
      done: 1,
      total: 1,
      planLoading: false,
      byMethod: { value_novelty: state("value_novelty", { findings: [finding()], total: 1 }) },
    };
    render(
      <InvestigateRail
        caseId="c1"
        timelineId="t1"
        onSelectFinding={onSelectFinding}
        onOpenTools={() => {}}
        onSelectEvent={() => {}}
      />,
      { wrapper },
    );
    screen.getByText(/curl\/7\.68\.0/).click();
    expect(onSelectFinding).toHaveBeenCalledWith("value_novelty", 0);
  });

  it("says the timeline is empty rather than that nothing was found", () => {
    // "No findings under this scope" on a timeline with no events reads as an
    // all-clear. Nothing was scanned; there was nothing to scan.
    renderRail({}, { stillIngesting: false, nothingToAnalyse: true });
    expect(screen.getByText("No events in this timeline yet.")).toBeInTheDocument();
    expect(screen.queryByText(/No findings under this scope/i)).toBeNull();
    expect(screen.getByRole("link", { name: /case overview/i })).toHaveAttribute(
      "href",
      "/cases/c1",
    );
  });

  it("says the sources are still ingesting rather than that there is nothing", () => {
    renderRail({}, { stillIngesting: true, nothingToAnalyse: true });
    expect(screen.getByText("This timeline's sources are still ingesting.")).toBeInTheDocument();
  });

  it("makes no all-clear claim while the plan is still resolving", () => {
    // Nothing is runnable during plan load, so `done === total` is vacuously
    // true — the empty state must not read that as "everything checked, clear".
    renderRail({ done: 0, total: 0, planLoading: true });
    expect(screen.queryByText(/No findings under this scope/i)).toBeNull();
    expect(screen.queryByText(/No method applies/i)).toBeNull();
  });

  it("distinguishes nothing-found from nothing-ran", () => {
    renderRail({ done: 0, total: 0, planLoading: false });
    expect(screen.getByText(/No method applies to this data yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/No findings under this scope/i)).toBeNull();
  });

  it("makes the all-clear claim only about the preset on screen", () => {
    // The sweep's global done/total say nothing about what this preset shows.
    // Reading them here let "Changed vs. baseline" — whose comparison methods
    // are gated off without a baseline — inherit an all-clear from methods it
    // hides, asserting a clean sweep for two methods that never ran.
    renderRail({
      done: 2,
      total: 2,
      byMethod: {
        value_novelty: state("value_novelty", { findings: [finding()], total: 1 }),
        frequency: state("frequency", {
          status: "not_applicable",
          plan: {
            method: "frequency",
            status: "not_applicable",
            reason: "no baseline is set",
            reason_facts: {},
            cost_class: "cheap",
          },
        }),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Changed vs. baseline" }));
    expect(screen.queryByText(/No findings under this scope/i)).toBeNull();
    expect(screen.getByText(/No method applies to this data yet/i)).toBeInTheDocument();
  });

  it("still says nothing-found when this preset's methods did all run", () => {
    renderRail({
      done: 1,
      total: 1,
      byMethod: { value_novelty: state("value_novelty") },
    });
    fireEvent.click(screen.getByRole("button", { name: "Unusual values" }));
    expect(screen.getByText(/No findings under this scope/i)).toBeInTheDocument();
  });

  it("renders a method's error without hiding the rest of the stream", () => {
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", { findings: [finding()], total: 1 }),
        charset: state("charset", { error: true }),
      },
    });
    expect(screen.getByTestId("method-errors")).toHaveTextContent(/charset|Charset/i);
    expect(screen.getByText(/curl\/7\.68\.0/)).toBeInTheDocument();
  });

  it("offers a way back to a dismissed finding", () => {
    // Dismissal is presentation-only and reversible on the server. With no
    // reveal anywhere in the UI, a mis-click removes a finding from every
    // surface permanently.
    const setIncludeDismissed = vi.fn();
    dismissed.current = { includeDismissed: false, setIncludeDismissed };
    renderRail({});
    screen.getByTestId("toggle-dismissed").click();
    expect(setIncludeDismissed).toHaveBeenCalledWith(true);
  });
});

describe("scope provenance on a row", () => {
  it("marks a verdict reached under another comparison instead of badging it", () => {
    // `confirmed` is the one disposition kind whose identity includes the
    // scope, so a February verdict is not a claim about March. Badging it as
    // confirmed here would disable Confirm and make the second claim
    // unreachable; showing nothing at all would hide that the finding has
    // already been escalated somewhere.
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding({ confirmed_other_scope: true })],
          total: 1,
        }),
      },
    });
    expect(screen.getByTestId("confirmed-other-scope")).toHaveTextContent("confirmed elsewhere");
    expect(screen.queryByText(/^confirmed$/)).toBeNull();
  });

  it("badges a verdict reached under the comparison on screen", () => {
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding({ confirmed: true })],
          total: 1,
        }),
      },
    });
    expect(screen.getByText("confirmed")).toBeInTheDocument();
    expect(screen.queryByTestId("confirmed-other-scope")).toBeNull();
  });
});

describe("named techniques", () => {
  const HIT = {
    ruleKey: "psexec_service_install",
    title: "psexec service install",
    level: "high",
    matchCount: 3,
    tag: "sigma: psexec service install",
  };

  afterEach(() => {
    sigma.current = [];
  });

  it("fills the strongest group with Sigma hits", () => {
    // The group shipped structurally empty: no method in the registry carries
    // `evidenceClass: "named"`, so the rail reserved its strongest slot for
    // something nothing could ever put there.
    sigma.current = [HIT];
    renderRail({});
    expect(screen.getByText(/Named techniques/)).toBeInTheDocument();
    expect(screen.getByText("psexec service install")).toBeInTheDocument();
  });

  it("keeps the group out of the rail when no run has hit anything", () => {
    renderRail({});
    expect(screen.queryByText(/Named techniques/)).toBeNull();
  });

  it("counts a Sigma hit as a finding, so the rail does not claim it is clear", () => {
    sigma.current = [HIT];
    renderRail({ done: 2, total: 2 });
    expect(screen.queryByText(/No findings under this scope/i)).toBeNull();
  });

  it("drills a rule's hits into the grid with the tag the Tools sheet uses", () => {
    sigma.current = [HIT];
    const onTagFilter = vi.fn();
    renderRail({}, readiness.current, { onTagFilter });
    fireEvent.click(screen.getByTestId("sigma-drill"));
    expect(onTagFilter).toHaveBeenCalledWith("sigma: psexec service install");
  });

  it("filters to Sigma alone under Known-bad, and away from it under the rest", () => {
    sigma.current = [HIT];
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", { findings: [finding()], total: 1 }),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Known-bad" }));
    expect(screen.getByText("psexec service install")).toBeInTheDocument();
    expect(screen.queryByText(/curl\/7\.68\.0/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Unusual values" }));
    expect(screen.queryByText("psexec service install")).toBeNull();
    expect(screen.getByText(/curl\/7\.68\.0/)).toBeInTheDocument();
  });
});
