/**
 * ExplorerPage routine-collapse wiring (#147).
 *
 * The resolver (lib/routineCollapse.ts) is unit-tested; what broke in #147 was
 * the *wiring* — the disposition-derived collapse never reached the events
 * request. This mounts the page and asserts the request-level truth:
 *
 * 1. The events query waits for the disposition set — no uncollapsed first
 *    fetch that flashes muted events and burns a ClickHouse scan on every
 *    load, only to be refetched collapsed a moment later.
 * 2. Once issued, the request's filters carry `collapseRoutine` whenever a
 *    routine disposition exists.
 *
 * Presentational children (grid, histogram, rails, panels) are stubbed: they
 * have their own tests, and this test's subject is the page's query wiring.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useScrollPositionStore } from "@/stores/scrollPosition";
import { useAgentStore } from "@/stores/agent";
import type { Disposition, Event, EventFilters, EventPage } from "@/api/types";

const eventsListMock = vi.fn();
const getByIdMock = vi.fn();
const dispositionsListMock = vi.fn();

// Latest props the (stubbed) children were rendered with — lets a test drive
// onExpand/onJumpToTime/onChange/onApplyFilters and read back what the grid
// received.
const captures = vi.hoisted(() => ({
  grid: null as null | Record<string, unknown>,
  detail: null as null | Record<string, unknown>,
  rail: null as null | Record<string, unknown>,
  agent: null as null | Record<string, unknown>,
}));

vi.mock("@/api/events", async () => {
  const actual = await vi.importActual<typeof import("@/api/events")>("@/api/events");
  return {
    ...actual,
    eventsApi: {
      ...actual.eventsApi,
      list: (...args: unknown[]) => eventsListMock(...args),
      getById: (...args: unknown[]) => getByIdMock(...args),
      mergedTags: async () => [],
      artifacts: async () => [],
      fields: async () => ({ top_level: [], attributes: [] }),
    },
  };
});
vi.mock("@/api/dispositions", async () => {
  const actual = await vi.importActual<typeof import("@/api/dispositions")>("@/api/dispositions");
  return {
    ...actual,
    dispositionsApi: {
      ...actual.dispositionsApi,
      list: (...args: unknown[]) => dispositionsListMock(...args),
    },
  };
});
vi.mock("@/api/annotations", async () => {
  const actual = await vi.importActual<typeof import("@/api/annotations")>("@/api/annotations");
  return {
    ...actual,
    annotationsApi: {
      ...actual.annotationsApi,
      listForTimeline: async () => [],
      listDistinctTags: async () => [],
    },
  };
});
vi.mock("@/api/timelines", async () => {
  const actual = await vi.importActual<typeof import("@/api/timelines")>("@/api/timelines");
  return {
    ...actual,
    timelinesApi: {
      ...actual.timelinesApi,
      get: async () => ({ id: "t1", case_id: "c1", name: "T1", source_ids: ["s1"] }),
      listSources: async () => [],
    },
  };
});
vi.mock("@/api/views", async () => {
  const actual = await vi.importActual<typeof import("@/api/views")>("@/api/views");
  return {
    ...actual,
    viewsApi: { ...actual.viewsApi, list: async () => [] },
  };
});
vi.mock("@/api/baselines", async () => {
  const actual = await vi.importActual<typeof import("@/api/baselines")>("@/api/baselines");
  return {
    ...actual,
    baselinesApi: { ...actual.baselinesApi, list: async () => ({ baselines: [] }) },
  };
});
// Agent available so the apply-a-finding seam can be driven (see the
// agent-apply seed-parity test); the panel itself is stubbed below.
vi.mock("@/api/health", () => ({
  useHealth: () => ({ data: { agent_available: true } }),
}));
vi.mock("@/hooks/useCaseStream", () => ({
  useCaseStream: () => undefined,
}));

// Presentational children stubbed — the page's query wiring is the subject.
// The grid/detail stubs capture their latest props so a test can drive
// onExpand/onJumpToTime and read back what the grid was handed.
vi.mock("@/components/explorer/EventGrid", () => ({
  EventGrid: (props: Record<string, unknown>) => {
    captures.grid = props;
    return null;
  },
}));
vi.mock("@/components/explorer/TimelineHistogram", () => ({
  TimelineHistogram: () => null,
}));
vi.mock("@/components/explorer/FilterRail", () => ({
  FilterRail: (props: Record<string, unknown>) => {
    captures.rail = props;
    return null;
  },
}));
vi.mock("@/components/explorer/FilterChips", () => ({
  FilterChips: () => null,
}));
vi.mock("@/components/explorer/EventDetailPanel", () => ({
  EventDetailPanel: (props: Record<string, unknown>) => {
    captures.detail = props;
    return null;
  },
}));
vi.mock("@/components/analysis/InvestigatePanel", () => ({
  InvestigatePanel: () => null,
}));
vi.mock("@/components/agent/AgentPanel", () => ({
  AgentPanel: (props: Record<string, unknown>) => {
    captures.agent = props;
    return null;
  },
}));
vi.mock("@/components/viz/FieldHistogramModal", () => ({
  FieldHistogramModal: () => null,
}));

import { ExplorerPage } from "@/pages/ExplorerPage";

const PAGE: EventPage = {
  total: 0,
  offset: 0,
  limit: 100,
  events: [],
  has_more_after: false,
  has_more_before: false,
  next_cursor: null,
  prev_cursor: null,
  routine_collapsed_count: 0,
};

function routineDisposition(id: string): Disposition {
  return {
    id,
    case_id: "c1",
    timeline_id: "t1",
    kind: "routine",
    detector: "log_template",
    field: "template_id",
    value: "4736",
    source_id: null,
    event_id: null,
    note: null,
    details: null,
    created_by: null,
    created_at: null,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/cases/c1/timelines/t1"]}>
          <Routes>
            <Route path="/cases/:caseId/timelines/:timelineId" element={<ExplorerPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  eventsListMock.mockReset().mockResolvedValue(PAGE);
  getByIdMock.mockReset();
  dispositionsListMock.mockReset();
  captures.grid = null;
  captures.detail = null;
  captures.rail = null;
  captures.agent = null;
  // Both stores are module-level singletons shared across tests.
  useScrollPositionStore.getState().setCurrentPositionTs(null);
  useAgentStore.getState().setPanelOpen(false);
});

/** The filters each `eventsApi.list` call was made with, oldest first. */
function requestedFilters(): Record<string, unknown>[] {
  return eventsListMock.mock.calls.map((c) => (c[2] ?? {}) as Record<string, unknown>);
}

/** The `eventsApi.list` calls made with a `before` cursor (the seeded pages). */
function anchoredCalls() {
  return eventsListMock.mock.calls.filter(
    (c) => (c[4] as { before?: string } | undefined)?.before,
  );
}

/**
 * Drive "locate this event in the timeline" the way the UI does: expand the
 * row, then hit the detail panel's locate button.
 */
async function locate(target: Event) {
  await act(async () => {
    (captures.grid!.onExpand as (e: Event) => void)(target);
  });
  await waitFor(() => expect(captures.detail).not.toBeNull());
  await act(async () => {
    await (captures.detail!.onJumpToTime as (ts: string, id: string) => Promise<void>)(
      target.timestamp!,
      target.event_id,
    );
  });
}

function event(id: string, ts: string): Event {
  return {
    event_id: id,
    timestamp: ts,
    source_id: "s1",
    message: id,
    artifact: null,
    tags: [],
    attributes: {},
  } as unknown as Event;
}

describe("ExplorerPage routine-collapse wiring", () => {
  it("waits for the disposition set, then queries collapsed — never the uncollapsed superset first", async () => {
    let resolveDispositions!: (v: { dispositions: Disposition[] }) => void;
    dispositionsListMock.mockReturnValue(
      new Promise((resolve) => {
        resolveDispositions = resolve;
      }),
    );

    renderPage();

    // Grace period: the events query must not fire while dispositions are
    // still loading — this uncollapsed fetch was the #147 flash.
    await new Promise((r) => setTimeout(r, 50));
    expect(eventsListMock).not.toHaveBeenCalled();

    resolveDispositions({ dispositions: [routineDisposition("d1")] });

    await waitFor(() => expect(eventsListMock).toHaveBeenCalled());
    const filters = eventsListMock.mock.calls[0][2] as Record<string, unknown>;
    expect(filters.collapseRoutine).toBe(true);
  });

  it("queries uncollapsed when no routine disposition exists", async () => {
    dispositionsListMock.mockResolvedValue({ dispositions: [] });

    renderPage();

    await waitFor(() => expect(eventsListMock).toHaveBeenCalled());
    for (const call of eventsListMock.mock.calls) {
      expect((call[2] as Record<string, unknown>).collapseRoutine).toBeFalsy();
    }
  });

  // #150: with collapse on, "locate this event" seeded the cache under a
  // hardcoded `{}` key that no longer matched the collapse-aware live key, so
  // the anchor page never reached the grid and nothing scrolled. The fix seeds
  // the *current* key. Here the located event E0 is muted (the filtered probe
  // returns it hidden), its "after" neighbour E2 comes only from the seek — so
  // E2 appearing in the grid proves the seed landed on the key the grid reads.
  it("locate under collapse seeds the grid (target reachable) and flags it hidden", async () => {
    const E0 = event("E0", "2026-01-01T00:00:00Z");
    const E2 = event("E2", "2026-01-01T00:00:02Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    getByIdMock.mockResolvedValue(E0);
    eventsListMock.mockImplementation(
      (
        _c: string,
        _t: string,
        filters: Record<string, unknown> | undefined,
        _signal: unknown,
        cursor: { before?: string; after?: string } | undefined,
      ) => {
        const f = filters ?? {};
        if (cursor?.after) return Promise.resolve({ ...PAGE, events: [E2] });
        if (cursor?.before) return Promise.resolve({ ...PAGE, events: [] });
        // Filtered membership probe for the target → empty means "hidden".
        if (Array.isArray(f.ids) && (f.ids as string[]).includes("E0")) {
          return Promise.resolve({ ...PAGE, events: [] });
        }
        return Promise.resolve({ ...PAGE, events: [E0] });
      },
    );

    renderPage();

    // Initial collapsed page loaded, grid shows just E0.
    await waitFor(() => {
      expect(captures.grid).not.toBeNull();
      expect((captures.grid!.events as Event[]).map((e) => e.event_id)).toEqual(["E0"]);
    });

    // Open the detail panel for E0, then trigger its "locate".
    await act(async () => {
      (captures.grid!.onExpand as (e: Event) => void)(E0);
    });
    await waitFor(() => expect(captures.detail).not.toBeNull());
    await act(async () => {
      await (captures.detail!.onJumpToTime as (ts: string, id: string) => Promise<void>)(
        E0.timestamp!,
        "E0",
      );
    });

    // The seeked anchor page (E0 spliced from getById + E2 from the "after"
    // neighbour) reached the grid, and E0 is flagged as normally hidden.
    await waitFor(() => {
      expect((captures.grid!.events as Event[]).map((e) => e.event_id)).toContain("E2");
      expect(captures.grid!.locatedHiddenId).toBe("E0");
    });

    // Every events request in this flow carried collapse — no seek ever
    // silently dropped it (the key-parity guarantee).
    for (const call of eventsListMock.mock.calls) {
      expect((call[2] as Record<string, unknown>).collapseRoutine).toBe(true);
    }
  });

  // Locate keeps the analyst's filters now (it used to clear them), so the
  // neighbour pages must be fetched *through* those filters — otherwise the
  // rows around the target would ignore the view the analyst is working in.
  it("locate fetches its neighbour pages through the active filters", async () => {
    const E0 = event("E0", "2026-01-01T00:00:00Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    getByIdMock.mockResolvedValue(E0);
    eventsListMock.mockResolvedValue({ ...PAGE, events: [E0] });

    renderPage();
    await waitFor(() => expect(captures.rail).not.toBeNull());

    await act(async () => {
      (captures.rail!.onChange as (f: EventFilters) => void)({ artifact: "logon" });
    });
    await waitFor(() => expect(requestedFilters().some((f) => f.artifact === "logon")).toBe(true));

    eventsListMock.mockClear();
    await locate(E0);

    // Neighbour pages + membership probe: every one carries the filter and
    // collapse. `getById` is the deliberate exception — it fetches the target
    // raw so a hidden event can still be force-included.
    expect(eventsListMock.mock.calls.length).toBeGreaterThan(0);
    for (const f of requestedFilters()) {
      expect(f.artifact).toBe("logon");
      expect(f.collapseRoutine).toBe(true);
    }
  });

  // The counterpart of the hidden case: when the probe finds the target, the
  // grid must not be told it is an exception to the view.
  it("locate leaves locatedHiddenId null when the view already shows the target", async () => {
    const E0 = event("E0", "2026-01-01T00:00:00Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    getByIdMock.mockResolvedValue(E0);
    // Probe and every other request return the event → it is visible.
    eventsListMock.mockResolvedValue({ ...PAGE, events: [E0] });

    renderPage();
    await waitFor(() => expect(captures.grid).not.toBeNull());

    await locate(E0);

    expect(captures.grid!.locatedHiddenId).toBeNull();
  });

  // "Normally hidden" is a claim about the current view. Revealing routine
  // events makes it false, so the marker must expire with the overlay rather
  // than sitting on the row asserting something untrue.
  it("clears the hidden marker when routine collapse is revealed", async () => {
    const E0 = event("E0", "2026-01-01T00:00:00Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    getByIdMock.mockResolvedValue(E0);
    eventsListMock.mockImplementation(
      (_c: string, _t: string, filters: Record<string, unknown> | undefined) => {
        const f = filters ?? {};
        // Hidden only while collapse is on.
        if (Array.isArray(f.ids) && f.collapseRoutine) return Promise.resolve({ ...PAGE, events: [] });
        return Promise.resolve({ ...PAGE, events: [E0] });
      },
    );

    const { getByTestId } = renderPage();
    await waitFor(() => expect(captures.grid).not.toBeNull());
    await locate(E0);
    await waitFor(() => expect(captures.grid!.locatedHiddenId).toBe("E0"));

    await act(async () => {
      fireEvent.click(getByTestId("routine-collapse-toggle"));
    });

    await waitFor(() => expect(captures.grid!.locatedHiddenId).toBeNull());
  });

  // The other half of #150: the soft anchor ("keep my scroll position when I
  // add a filter") built its seed key by hand and dropped `collapseRoutine`,
  // so the anchored page landed in a cache entry the grid never read and the
  // grid silently reset to the top.
  it("soft-anchor seeds its page under the collapse-aware key", async () => {
    const E5 = event("E5", "2026-01-01T00:00:05Z");
    const E6 = event("E6", "2026-01-01T00:00:06Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    eventsListMock.mockImplementation(
      (
        _c: string,
        _t: string,
        filters: Record<string, unknown> | undefined,
        _signal: unknown,
        cursor: { before?: string; after?: string } | undefined,
      ) => {
        // Only the anchored (`before`-cursor) fetch returns E6 — its presence
        // in the grid proves the seed landed on the key the grid reads.
        if (cursor?.before) return Promise.resolve({ ...PAGE, events: [E6] });
        void filters;
        return Promise.resolve({ ...PAGE, events: [E5] });
      },
    );

    renderPage();
    // Wait for the initial collapsed page — an analyst can't have scrolled into
    // a result set that hasn't loaded, and collapse must already be resolved.
    await waitFor(() => {
      expect((captures.grid?.events as Event[] | undefined)?.length).toBe(1);
    });

    // The analyst has scrolled into the result set.
    act(() => {
      useScrollPositionStore.getState().setCurrentPositionTs("2026-01-01T00:00:05Z");
    });

    await act(async () => {
      (captures.rail!.onChange as (f: EventFilters) => void)({ artifact: "logon" });
    });

    await waitFor(() => {
      expect((captures.grid!.events as Event[]).map((e) => e.event_id)).toContain("E6");
    });

    const anchored = anchoredCalls();
    expect(anchored.length).toBeGreaterThan(0);
    for (const call of anchored) {
      const f = call[2] as Record<string, unknown>;
      expect(f.collapseRoutine).toBe(true);
      expect(f.artifact).toBe("logon");
    }
  });

  // Applying an agent finding is the hardest case for seed/live key parity: the
  // finding's filter object carries session-overlay fields (`ids`,
  // `collapseRoutine`) that the URL deliberately drops, *and* it sets those
  // overlays in the same React batch as the filter change, so the seek can read
  // neither from state nor from the raw filter object. The seed therefore has
  // to reproduce the live key the way the live query builds it — URL
  // round-trip + the same overlay helper. E6 only ever comes from the anchored
  // fetch, so seeing it in the grid is the parity proof.
  it("agent-apply seeds a page the grid actually reads", async () => {
    const E5 = event("E5", "2026-01-01T00:00:05Z");
    const E6 = event("E6", "2026-01-01T00:00:06Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    eventsListMock.mockImplementation(
      (
        _c: string,
        _t: string,
        _filters: Record<string, unknown> | undefined,
        _signal: unknown,
        cursor: { before?: string; after?: string } | undefined,
      ) =>
        Promise.resolve(
          cursor?.before ? { ...PAGE, events: [E6] } : { ...PAGE, events: [E5] },
        ),
    );

    renderPage();
    act(() => {
      useAgentStore.getState().setPanelOpen(true);
    });
    await waitFor(() => {
      expect(captures.agent).not.toBeNull();
      expect((captures.grid?.events as Event[] | undefined)?.length).toBe(1);
    });

    act(() => {
      useScrollPositionStore.getState().setCurrentPositionTs("2026-01-01T00:00:05Z");
    });

    // A finding that ran *uncollapsed* over an explicit event_id allowlist —
    // both overlays differ from the page's current state.
    await act(async () => {
      (captures.agent!.onApplyFilters as (f: EventFilters) => void)({
        artifact: "logon",
        ids: ["A1", "A2"],
        collapseRoutine: false,
      } as EventFilters);
    });

    await waitFor(() => {
      expect((captures.grid!.events as Event[]).map((e) => e.event_id)).toContain("E6");
    });
    // And the anchored fetch ran through the finding's own overlays, not the
    // page's pre-apply state.
    for (const call of anchoredCalls()) {
      const f = call[2] as Record<string, unknown>;
      expect(f.ids).toEqual(["A1", "A2"]);
      expect(f.collapseRoutine).toBeFalsy();
    }
  });

  // A jump and the soft anchor now seed the *same* query key (the jump keeps
  // the analyst's filters instead of clearing them, which is what used to move
  // it onto a key of its own). So a soft-anchor fetch still in flight when the
  // analyst hits "locate" would land *after* the jump's anchor page and
  // overwrite it — the grid would jump back to where they were scrolled.
  //
  // Reproduced by holding the soft anchor's fetch open (its cursor has no
  // event id — `"<ts>,"` — which is what distinguishes it from the jump's
  // neighbour pages) until after the jump has seeded.
  it("a soft anchor resolving late can't overwrite a jump's page", async () => {
    const E0 = event("E0", "2026-01-01T00:00:00Z");
    const E9 = event("E9", "2026-01-01T00:00:09Z");
    dispositionsListMock.mockResolvedValue({ dispositions: [routineDisposition("d1")] });
    getByIdMock.mockResolvedValue(E0);
    let releaseSoftAnchor!: () => void;
    const softAnchorFetched = new Promise<void>((resolve) => {
      releaseSoftAnchor = resolve;
    });
    eventsListMock.mockImplementation(
      (
        _c: string,
        _t: string,
        _filters: Record<string, unknown> | undefined,
        _signal: unknown,
        cursor: { before?: string; after?: string } | undefined,
      ) => {
        if (cursor?.after) return Promise.resolve({ ...PAGE, events: [] });
        if (cursor?.before?.endsWith(",")) {
          // The soft anchor's page (E9, far from the target) — held open, then
          // resolved after the jump has seeded.
          return softAnchorFetched.then(() => ({ ...PAGE, events: [E9] }));
        }
        if (cursor?.before) return Promise.resolve({ ...PAGE, events: [] });
        return Promise.resolve({ ...PAGE, events: [E0] });
      },
    );

    renderPage();
    await waitFor(() => {
      expect((captures.grid?.events as Event[] | undefined)?.length).toBe(1);
    });
    act(() => {
      useScrollPositionStore.getState().setCurrentPositionTs("2026-01-01T00:00:09Z");
    });

    // Filter change → soft-anchor seek starts and hangs on its fetch.
    await act(async () => {
      (captures.rail!.onChange as (f: EventFilters) => void)({ artifact: "logon" });
    });
    await waitFor(() => expect(anchoredCalls().length).toBeGreaterThan(0));

    // Analyst hits "locate" while that fetch is still outstanding.
    await locate(E0);
    await waitFor(() => {
      expect((captures.grid!.events as Event[]).map((e) => e.event_id)).toContain("E0");
    });

    // The stale soft anchor now comes back. It must not win.
    await act(async () => {
      releaseSoftAnchor();
      await new Promise((r) => setTimeout(r, 20));
    });
    const ids = (captures.grid!.events as Event[]).map((e) => e.event_id);
    expect(ids).toContain("E0");
    expect(ids).not.toContain("E9");
  });
});
