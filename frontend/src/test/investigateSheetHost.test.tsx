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

vi.mock("@/components/analysis/InvestigateSheet", () => ({
  InvestigateSheet: ({
    mode,
    onRun,
  }: {
    mode: string;
    onRun?: (p: Record<string, unknown>) => void;
  }) => (
    <button data-testid={`sheet-${mode}`} onClick={() => onRun?.({ fields: "attr:user_agent" })}>
      run
    </button>
  ),
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
    />,
    { wrapper },
  );
}

describe("InvestigateSheetHost run parameters", () => {
  it("drops a custom run when the same method's finding is opened", () => {
    calls.current = [];
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
