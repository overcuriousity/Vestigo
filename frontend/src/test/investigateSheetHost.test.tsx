/**
 * InvestigateSheetHost owns one piece of state — the parameters a method was
 * last *run* with — and it has to reset that whenever the sheet changes what it
 * is showing.
 *
 * The case this covers: run a method from its own sheet with custom knobs, then
 * click one of that same method's rows in the rail. The method never changed,
 * so a reset keyed only on the method id does not fire, and the finding view
 * keys on the custom run while the rail addressed a rank in the plain sweep —
 * showing a different finding than the one clicked, or none at all.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InvestigateSheetHost, type SheetRequest } from "@/components/analysis/InvestigateSheetHost";

/** Every `useMethodFindings` call, so the params it keyed on are inspectable. */
const calls = vi.hoisted(() => ({ current: [] as Record<string, unknown>[] }));

vi.mock("@/hooks/useMethodFindings", () => ({
  useMethodFindings: (
    _c: string,
    _t: string,
    method: string,
    opts: { enabled: boolean; params?: Record<string, unknown> },
  ) => {
    calls.current.push({ method, ...opts });
    return {
      // Enough of a result set for finding mode to render — the rail addresses
      // a rank, and the host falls back to method mode when that rank is
      // missing, which would make the rank-change case untestable.
      data: {
        results: [{ rank: 0 }, { rank: 1 }, { rank: 2 }],
        scope: { frame: "self", baseline_id: null },
      },
      isFetching: false,
      isError: false,
    };
  },
  METHOD_LIMIT: 50,
}));

const configured = vi.hoisted(() => ({ entries: [] as Record<string, unknown>[] }));
vi.mock("@/hooks/useTimelineDetectors", () => ({
  useTimelineDetectors: () => ({
    entries: configured.entries,
    byMethod: new Map(configured.entries.map((e) => [e.method, e])),
    isLoaded: true,
    set: async () => ({}),
    remove: async () => ({}),
    canEdit: true,
    isSaving: false,
    saveError: null,
  }),
  scopeOf: (e: { frame: string; baseline_id: string | null }) =>
    e.frame === "baseline" && e.baseline_id
      ? { frame: "baseline", baseline_id: e.baseline_id }
      : { frame: "self" },
}));

vi.mock("@/hooks/useScopeChange", () => ({
  useScopeChange: () => ({
    pending: null,
    currentScope: { frame: "self", baseline_id: null, baseline_name: null },
    affectedVerdicts: 0,
    request: () => {},
    cancel: () => {},
    confirm: () => {},
  }),
}));

/** Every render of the sheet, so the props it was handed are inspectable. */
const sheets = vi.hoisted(() => ({ current: [] as Record<string, unknown>[] }));
vi.mock("@/components/analysis/InvestigateSheet", () => ({
  InvestigateSheet: ({
    mode,
    initialParams,
    onRun,
  }: {
    mode: string;
    initialParams?: Record<string, unknown>;
    onRun?: (p: Record<string, unknown>) => void;
  }) => {
    sheets.current.push({ mode, initialParams });
    return (
      <button data-testid={`sheet-${mode}`} onClick={() => onRun?.({ fields: "attr:user_agent" })}>
        run
      </button>
    );
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function renderHost(sheet: SheetRequest) {
  return render(
    <InvestigateSheetHost
      caseId="c1"
      timelineId="t1"
      railWidth={320}
      sheet={sheet}
      onClose={() => {}}
      onOpenMethod={() => {}}
      onRunMethod={() => {}}
      onAddDetector={() => {}}
    />,
    { wrapper },
  );
}

describe("InvestigateSheetHost configured entries", () => {
  it("opens a finding under the configured entry's params and scope, so it matches the rail", () => {
    calls.current = [];
    configured.entries = [
      {
        method: "value_novelty",
        params: { fields: ["user"] },
        frame: "baseline",
        baseline_id: "b1",
      },
    ];
    renderHost({ kind: "finding", method: "value_novelty", rank: 0 });
    expect(calls.current.at(-1)).toMatchObject({
      method: "value_novelty",
      enabled: true,
      params: { fields: ["user"] },
      scope: { frame: "baseline", baseline_id: "b1" },
    });
    configured.entries = [];
  });

  it("opens the knob form on the params that produced the finding, not on defaults", () => {
    // A form showing "auto" under a finding computed from stored settings
    // offers to re-run a question nobody asked: pressing Run with these sent
    // empty params, which is a different scan presented as a re-run of the one
    // on screen.
    sheets.current = [];
    configured.entries = [
      {
        method: "sequence_novelty",
        params: { series_field: "attr:computer_name" },
        frame: "baseline",
        baseline_id: "b1",
      },
    ];
    renderHost({ kind: "finding", method: "sequence_novelty", rank: 0 });
    expect(sheets.current.at(-1)).toMatchObject({
      mode: "finding",
      initialParams: { series_field: "attr:computer_name" },
    });
    configured.entries = [];
  });

  it("keeps the entry's own scope when its knobs are re-run from the finding", () => {
    // "Run with these" tweaks a configured detector's parameters. Falling back
    // to the panel frame asks a different question — and for a baseline-framed
    // method, one with no baseline to answer it.
    calls.current = [];
    configured.entries = [
      {
        method: "proportion_shift",
        params: { fields: ["user"] },
        frame: "baseline",
        baseline_id: "b1",
      },
    ];
    const { getByTestId } = renderHost({ kind: "finding", method: "proportion_shift", rank: 0 });
    fireEvent.click(getByTestId("sheet-finding"));
    expect(calls.current.at(-1)).toMatchObject({
      params: { fields: "attr:user_agent" },
      scope: { frame: "baseline", baseline_id: "b1" },
    });
    configured.entries = [];
  });

  it("stops querying and closes when the detector is removed while its finding is open", () => {
    // The rail stays interactive beside the sheet. With the entry gone the
    // query used to fall back to the panel scope with empty params — a fresh,
    // unprompted scan for a detector that was just deleted, answering a
    // different question than the row that was clicked.
    calls.current = [];
    configured.entries = [
      { method: "value_novelty", params: { fields: ["user"] }, frame: "self", baseline_id: null },
    ];
    const onClose = vi.fn();
    const sheet: SheetRequest = { kind: "finding", method: "value_novelty", rank: 0 };
    const props = {
      caseId: "c1",
      timelineId: "t1",
      railWidth: 320,
      sheet,
      onClose,
      onOpenMethod: () => {},
      onRunMethod: () => {},
      onAddDetector: () => {},
    };
    const { rerender } = render(<InvestigateSheetHost {...props} />, { wrapper });
    expect(calls.current.at(-1)).toMatchObject({ enabled: true });

    configured.entries = [];
    rerender(<InvestigateSheetHost {...props} />);

    expect(calls.current.at(-1)).toMatchObject({ enabled: false });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("InvestigateSheetHost run parameters", () => {
  it("drops a custom run when the same method's finding is opened", () => {
    calls.current = [];
    configured.entries = [{ method: "value_novelty", params: {}, frame: "self", baseline_id: null }];
    const { rerender, getByTestId } = renderHost({ kind: "method", method: "value_novelty" });

    // Run it with knobs the analyst typed.
    fireEvent.click(getByTestId("sheet-method"));
    expect(calls.current.at(-1)).toMatchObject({
      enabled: true,
      params: { fields: "attr:user_agent" },
    });

    // Now click one of that method's rows in the rail. Same method — but the
    // rank the rail handed over indexes the plain sweep, so the query behind
    // the finding view must be the plain one too.
    rerender(
      <InvestigateSheetHost
        caseId="c1"
        timelineId="t1"
        railWidth={320}
        sheet={{ kind: "finding", method: "value_novelty", rank: 2 }}
        onClose={() => {}}
        onOpenMethod={() => {}}
        onRunMethod={() => {}}
        onAddDetector={() => {}}
      />,
    );
    // `toMatchObject` treats `{}` as "any object", so the params are compared
    // with `toEqual` — the vacuous version of this assertion passes against the
    // very bug it exists to catch.
    expect(calls.current.at(-1)).toMatchObject({ method: "value_novelty" });
    expect(calls.current.at(-1)!.params).toEqual({});
  });

  it("drops a custom run when a different rank of the same method is opened", () => {
    calls.current = [];
    configured.entries = [{ method: "value_novelty", params: {}, frame: "self", baseline_id: null }];
    const sheetAt = (rank: number): SheetRequest => ({
      kind: "finding",
      method: "value_novelty",
      rank,
    });
    const { rerender, getByTestId } = renderHost(sheetAt(0));

    // "Run with these" from *finding* mode: the sheet flips to method mode and
    // keeps the typed knobs.
    fireEvent.click(getByTestId("sheet-finding"));
    expect(calls.current.at(-1)!.params).toEqual({ fields: "attr:user_agent" });
    getByTestId("sheet-method");

    // Clicking a different row of the same method changes neither `kind` nor
    // `method`, so only the rank can tell the host to go back to the plain
    // sweep and render the row that was clicked.
    rerender(
      <InvestigateSheetHost
        caseId="c1"
        timelineId="t1"
        railWidth={320}
        sheet={sheetAt(1)}
        onClose={() => {}}
        onOpenMethod={() => {}}
        onRunMethod={() => {}}
        onAddDetector={() => {}}
      />,
    );
    expect(calls.current.at(-1)!.params).toEqual({});
    getByTestId("sheet-finding");
  });

  it("keeps autorun's empty params when the Tools sheet runs a method", () => {
    calls.current = [];
    renderHost({ kind: "method", method: "frequency", autorun: true });
    expect(calls.current.at(-1)).toMatchObject({ method: "frequency", enabled: true });
    expect(calls.current.at(-1)!.params).toEqual({});
  });
});
