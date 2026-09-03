/**
 * The rail's contract: run only what is configured, group findings by what
 * kind of claim they are, never present an unconfigured or still-pending
 * detector as an all-clear, and name each configured detector's scope.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
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

const detectors = vi.hoisted(() => ({
  entries: [] as Record<string, unknown>[],
  removed: [] as string[],
  canEdit: true,
}));
vi.mock("@/hooks/useTimelineDetectors", () => ({
  useTimelineDetectors: () => ({
    entries: detectors.entries,
    byMethod: new Map(detectors.entries.map((e) => [e.method, e])),
    set: async () => ({}),
    remove: async (m: string) => {
      detectors.removed.push(m);
      return {};
    },
    canEdit: detectors.canEdit,
    isSaving: false,
    saveError: null,
  }),
  scopeOf: () => ({ frame: "self" }),
}));
vi.mock("@/api/baselines", () => ({
  baselinesApi: { list: async () => ({ baselines: [{ id: "bl-1", name: "Feb 24 – Mar 1" }] }) },
}));

const sigma = vi.hoisted(() => ({ current: [] as unknown[] }));
vi.mock("@/hooks/useSigmaFindings", () => ({
  useSigmaFindings: () => ({
    findings: sigma.current,
    isLoading: false,
    available: true,
  }),
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

const entryOf = (id: string, over: Record<string, unknown> = {}) => ({
  method: id,
  params: {},
  frame: "self",
  baseline_id: null,
  added_by: null,
  added_at: "",
  ...over,
});

function state(id: string, over: Record<string, unknown> = {}) {
  return {
    meta: METHODS_BY_ID[id as keyof typeof METHODS_BY_ID],
    plan: {
      method: id,
      status: "applicable",
      reason: "",
      reason_facts: {},
      cost_class: "cheap",
    },
    status: "applicable",
    findings: [],
    total: 0,
    isLoading: false,
    pending: false,
    error: false,
    configured: true,
    entry: entryOf(id),
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
  // The configured list mirrors the sweep's `configured` states, as the real
  // hooks share one source.
  const byMethod = sweep.current.byMethod as Record<string, { configured?: boolean; entry?: unknown }>;
  detectors.entries = Object.values(byMethod)
    .filter((st) => st.configured && st.entry)
    .map((st) => st.entry as Record<string, unknown>);
  return render(
    <InvestigateRail
      caseId="c1"
      timelineId="t1"
      onSelectFinding={() => {}}
      onOpenTools={() => {}}
      onAddDetector={() => {}}
      onSelectEvent={() => {}}
      {...props}
    />,
    { wrapper },
  );
}

describe("InvestigateRail", () => {
  beforeEach(() => {
    readiness.current = { stillIngesting: false, nothingToAnalyse: false };
    dismissed.current = {
      includeDismissed: false,
      setIncludeDismissed: () => {},
    };
  });

  it("orders groups strongest-claim first", () => {
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding()],
          total: 1,
        }),
        log_template: state("log_template", {
          findings: [
            { template: "GET <PATH> <NUM>", count: 6, template_hash: "h1" },
          ],
          total: 1,
        }),
      },
    });
    const headings = screen
      .getAllByTestId("evidence-group")
      .map((el) => el.textContent);
    expect(headings[0]).toContain("Statistical outliers");
    expect(headings[1]).toContain("Exploration");
  });

  it("shows the empty state with the wizard entry when nothing is configured", () => {
    const onAddDetector = vi.fn();
    renderRail({ byMethod: {}, done: 0, total: 0 }, readiness.current, { onAddDetector });
    expect(screen.getByTestId("no-detectors")).toHaveTextContent("No detectors configured");
    expect(screen.queryByText(/No findings from the configured detectors/i)).toBeNull();
    fireEvent.click(screen.getByTestId("add-detector"));
    expect(onAddDetector).toHaveBeenCalledWith();
  });

  it("names each configured detector with its scope and count, editable and removable", async () => {
    const onAddDetector = vi.fn();
    detectors.removed = [];
    renderRail(
      {
        byMethod: {
          value_novelty: state("value_novelty", {
            findings: [finding()],
            total: 1,
            entry: entryOf("value_novelty", { frame: "baseline", baseline_id: "bl-1" }),
          }),
        },
      },
      readiness.current,
      { onAddDetector },
    );
    const chip = screen.getByTestId("detector-chip-value_novelty");
    expect(chip).toHaveTextContent("Rare values");
    expect(chip).toHaveTextContent("1");
    await screen.findByText(/Feb 24 – Mar 1/);
    fireEvent.click(within(chip).getByTitle("Edit"));
    expect(onAddDetector).toHaveBeenCalledWith("value_novelty");
    fireEvent.click(within(chip).getByTitle("Remove"));
    expect(detectors.removed).toEqual(["value_novelty"]);
  });

  it("hides edit and remove from read-only members", () => {
    detectors.canEdit = false;
    renderRail({ byMethod: { value_novelty: state("value_novelty") } });
    const chip = screen.getByTestId("detector-chip-value_novelty");
    expect(within(chip).queryByTitle("Remove")).toBeNull();
    expect(screen.queryByTestId("add-detector")).toBeNull();
    detectors.canEdit = true;
  });

  it("never renders a group for an unconfigured method", () => {
    renderRail({
      byMethod: {
        entropy: state("entropy", { configured: false, entry: undefined }),
      },
      done: 0,
      total: 0,
    });
    expect(screen.queryByTestId("detector-chip-entropy")).toBeNull();
    expect(screen.getByTestId("no-detectors")).toBeInTheDocument();
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
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding()],
          total: 1,
        }),
      },
    };
    render(
      <InvestigateRail
        caseId="c1"
        timelineId="t1"
        onSelectFinding={onSelectFinding}
        onOpenTools={() => {}}
        onAddDetector={() => {}}
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
    expect(
      screen.getByText("No events in this timeline yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No findings from the configured detectors/i)).toBeNull();
    expect(
      screen.getByRole("link", { name: /case overview/i }),
    ).toHaveAttribute("href", "/cases/c1");
  });

  it("says the sources are still ingesting rather than that there is nothing", () => {
    renderRail({}, { stillIngesting: true, nothingToAnalyse: true });
    expect(
      screen.getByText("This timeline's sources are still ingesting."),
    ).toBeInTheDocument();
  });

  it("makes no all-clear claim while a configured detector is still pending", () => {
    renderRail({
      done: 0,
      total: 1,
      byMethod: { entropy: state("entropy", { pending: true }) },
    });
    expect(screen.queryByText(/No findings from the configured detectors/i)).toBeNull();
  });

  it("says nothing-found only once every configured detector settled", () => {
    renderRail({ done: 1, total: 1, byMethod: { entropy: state("entropy") } });
    expect(
      screen.getByText(/No findings from the configured detectors/i),
    ).toBeInTheDocument();
  });

  it("keeps a barely-out-of-band finding out of the ranked feed, and says so", () => {
    // Rotation puts one row per method near the top, so without a floor a
    // finding 1.03 band widths out sits above one 46 band widths out. Held
    // back is not the same as not found: the count has to stay on screen and
    // the rows have to be one click away.
    const weak = {
      type: "entropy" as const,
      field: "attr:tcp_ack",
      value: "2212222122",
      entropy: 0.72,
      lower: 1.97,
      upper: 3.18,
      direction: "below" as const,
      score: 1.03,
      count: 1,
      event_id: "e2",
      event: null,
      first_seen: "2026-03-04T02:11:07Z",
      details: {},
    };
    renderRail({
      byMethod: { entropy: state("entropy", { findings: [weak], total: 1 }) },
    });
    expect(screen.queryByText(/2212222122/)).toBeNull();
    const summary = screen.getByTestId("weak-summary");
    expect(summary).toHaveTextContent("1 weaker finding");
    // And it is not an all-clear: the group still counts what it holds.
    expect(screen.queryByText(/No findings from the configured detectors/i)).toBeNull();

    fireEvent.click(summary);
    expect(screen.getByText(/2212222122/)).toBeInTheDocument();
  });

  it("leaves a method with no floor of its own untouched", () => {
    // `frequency` carries `z_threshold`, so its floor belongs in the run.
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding({ score: 0.4 })],
          total: 1,
        }),
      },
    });
    expect(screen.getByText(/curl\/7\.68\.0/)).toBeInTheDocument();
    expect(screen.queryByTestId("weak-summary")).toBeNull();
  });

  it("renders a method's error without hiding the rest of the stream", () => {
    renderRail({
      byMethod: {
        value_novelty: state("value_novelty", {
          findings: [finding()],
          total: 1,
        }),
        charset: state("charset", { error: true }),
      },
    });
    expect(screen.getByTestId("method-errors")).toHaveTextContent(
      /charset|Charset/i,
    );
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
    expect(screen.getByTestId("confirmed-other-scope")).toHaveTextContent(
      "confirmed elsewhere",
    );
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
    renderRail({ done: 0, total: 0 });
    expect(screen.queryByText(/No findings from the configured detectors/i)).toBeNull();
    expect(screen.queryByTestId("no-detectors")).toBeNull();
  });

  it("drills a rule's hits into the grid with the tag the Tools sheet uses", () => {
    sigma.current = [HIT];
    const onTagFilter = vi.fn();
    renderRail({}, readiness.current, { onTagFilter });
    fireEvent.click(screen.getByTestId("sigma-drill"));
    expect(onTagFilter).toHaveBeenCalledWith("sigma: psexec service install");
  });
});
