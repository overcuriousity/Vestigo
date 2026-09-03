/**
 * Declaring which fields a method reads.
 *
 * The field recommenders behind every method's "auto" mode type fields
 * syntactically: an HTTP status code parses as a number, so the numeric-range
 * detector offers it and reports every 500 as an outlier forever. The picker's
 * checkboxes could only correct that for one run — the correction died with the
 * component's state, and the next analyst never learned it had been made.
 *
 * These assertions are about the two halves of the durable version. It has to
 * be a *different* control from the checkbox (scoping a run and deciding what a
 * method reads are different questions), and it has to change the auto preview,
 * because a picker that keeps showing a field as checked after it was declared
 * off is claiming a scan that will not happen.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InvestigateSheet } from "@/components/analysis/InvestigateSheet";
import { TooltipProvider } from "@/components/ui/Tooltip";

vi.mock("@/hooks/useMethodFindings", () => ({
  useFindingsPage: () => ({ limit: 50, canRaise: true, raise: () => {} }),
  useMethodFindings: () => ({ data: undefined, isLoading: false, isError: false }),
  METHOD_LIMIT: 50,
}));

vi.mock("@/api/anomalies", () => ({
  anomaliesApi: {
    fields: async () => ({
      fields: [
        { token: "attr:src_ip", distinct: 42, coverage: 0.98, kind: "categorical", recommended: true },
        { token: "attr:status_code", distinct: 5, coverage: 0.9, kind: "categorical", recommended: true },
        { token: "attr:session", distinct: 9001, coverage: 0.4, kind: "identifier", recommended: false },
      ],
    }),
    numericFields: async () => ({ fields: [] }),
  },
}));

const timeline = vi.hoisted(() => ({
  overrides: {} as Record<string, Record<string, boolean>>,
}));
const patched = vi.hoisted(() => ({
  calls: [] as Record<string, Record<string, boolean>>[],
  /** Held open by the race test to keep a PATCH in flight across a second click. */
  gate: null as Promise<void> | null,
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({ id: "t1", case_id: "c1", field_overrides: timeline.overrides }),
    patchFieldOverrides: async (
      _c: string,
      _t: string,
      next: Record<string, Record<string, boolean>>,
    ) => {
      if (patched.gate) await patched.gate;
      patched.calls.push(next);
      timeline.overrides = next;
      return { id: "t1", case_id: "c1", field_overrides: next };
    },
  },
}));

vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access_level: "contribute" }) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

function renderMethod(methodId = "value_novelty") {
  return render(
    <InvestigateSheet
      caseId="c1"
      timelineId="t1"
      railWidth={360}
      mode="method"
      methodId={methodId as never}
      onClose={() => {}}
      onRun={() => {}}
      query={{ data: undefined, isFetching: false, isError: false } as never}
    />,
    { wrapper },
  );
}

async function openPicker() {
  fireEvent.click(screen.getByRole("button", { name: /fields/i }));
  return await screen.findByTestId("declare-attr:status_code");
}

describe("declaring a field for a method", () => {
  beforeEach(() => {
    timeline.overrides = {};
    patched.calls = [];
    patched.gate = null;
  });

  it("keeps a declaration made while the previous one is still in flight", async () => {
    // The chip row invites exactly this: ban one field, then the next. The
    // PATCH is a full replace, so a second edit built on the pre-mutation
    // snapshot would drop the first from the timeline *and* from the audit
    // row's previous/new pair.
    let release: () => void = () => {};
    patched.gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    renderMethod();
    fireEvent.click(await openPicker());
    fireEvent.click(screen.getByTestId("declare-attr:src_ip"));
    release();

    await waitFor(() => expect(patched.calls).toHaveLength(2));
    expect(patched.calls[1]).toEqual({
      value_novelty: { "attr:status_code": false, "attr:src_ip": false },
    });
  });

  it("declares a field off for everyone, not just this run", async () => {
    renderMethod();
    fireEvent.click(await openPicker());
    await waitFor(() => expect(patched.calls).toHaveLength(1));
    expect(patched.calls[0]).toEqual({ value_novelty: { "attr:status_code": false } });
  });

  it("cycles off → pinned → undeclared, so the recommender's answer stays reachable", async () => {
    timeline.overrides = { value_novelty: { "attr:status_code": false } };
    renderMethod();
    fireEvent.click(await openPicker());
    await waitFor(() => expect(patched.calls).toHaveLength(1));
    expect(patched.calls[0]).toEqual({ value_novelty: { "attr:status_code": true } });

    // And once more, back to undeclared — the method with nothing left declared
    // drops out entirely rather than lingering as an empty object.
    fireEvent.click(await screen.findByTestId("declare-attr:status_code"));
    await waitFor(() => expect(patched.calls).toHaveLength(2));
    expect(patched.calls[1]).toEqual({});
  });

  it("drops a declared-off field from the auto preview", async () => {
    // Otherwise the checked set claims a scan the backend will not run.
    timeline.overrides = { value_novelty: { "attr:status_code": false } };
    renderMethod();
    await openPicker();
    const chip = screen.getByTestId("declare-attr:status_code");
    expect(chip).toHaveAttribute("data-declared", "off");
    await waitFor(() =>
      expect(screen.getByText(/field.? selected/)).toHaveTextContent("1 field selected"),
    );
  });

  it("adds a pinned field to the auto preview the recommender left out", async () => {
    timeline.overrides = { value_novelty: { "attr:session": true } };
    renderMethod();
    await openPicker();
    expect(screen.getByTestId("declare-attr:session")).toHaveAttribute("data-declared", "on");
    await waitFor(() =>
      expect(screen.getByText(/fields selected/)).toHaveTextContent("3 fields selected"),
    );
  });

  it("discloses how many fields the timeline declares", async () => {
    // A declaration that narrows a scan without saying so is the one thing this
    // must never look like.
    timeline.overrides = { value_novelty: { "attr:status_code": false } };
    renderMethod();
    await openPicker();
    await waitFor(() => expect(screen.getByText(/1 declared/)).toBeInTheDocument());
  });

  it("keeps another method's declaration out of this one", async () => {
    // The whole point of declaring per method: a status code is meaningless to
    // numeric_range and an excellent value_novelty field.
    timeline.overrides = { numeric_range: { "attr:status_code": false } };
    renderMethod();
    await openPicker();
    expect(screen.getByTestId("declare-attr:status_code")).toHaveAttribute(
      "data-declared",
      "auto",
    );
  });
});
