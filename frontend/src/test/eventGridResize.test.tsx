/**
 * Column sizing and resizing, which is where react-table 9's feature API can
 * break the grid without breaking a type check.
 *
 * The grid enables exactly three features (`columnSizingFeature`,
 * `columnResizingFeature`, `columnVisibilityFeature`) and subscribes to one
 * state slice. If a feature were dropped, `getSize()` and the grip would
 * simply stop existing; if the subscribed slice were the wrong one, the
 * resize gesture would never look finished and no width would ever reach
 * localStorage. Both failures are silent at build time, and both are what
 * these tests are for.
 *
 * v9 renamed the live resize state: it is `columnResizing.isResizingColumn`,
 * not v8's `columnSizingInfo.isResizingColumn`.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { EventGrid } from "@/components/explorer/EventGrid";
import { useUiStore } from "@/stores/ui";
import type { Event } from "@/api/types";

function evt(i: number): Event {
  return {
    event_id: `e${i}`,
    case_id: "c1",
    source_id: "s1",
    timestamp: `2026-07-0${(i % 9) + 1}T00:00:00Z`,
    artifact: "webserver:access",
    artifact_long: "",
    display_name: "",
    message: `line ${i}`,
    timestamp_desc: "",
    tags: [],
    attributes: {},
  } as unknown as Event;
}

function renderGrid() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <EventGrid
          events={[evt(1), evt(2)]}
          total={2}
          annotations={new Map()}
          selectedIds={new Set()}
          caseId="c1"
          onToggleSelect={vi.fn()}
          onToggleSelectAll={vi.fn()}
          expandedId={null}
          onExpand={vi.fn()}
          onLoadMore={vi.fn()}
          onLoadEarlier={vi.fn()}
          hasPreviousPage={false}
          hasNextPage={false}
          isFetching={false}
          visibleColumns={["timestamp", "artifact", "message"]}
          onReorderColumns={vi.fn()}
          sortDir="desc"
          onSortToggle={vi.fn()}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** The resize grip of the first resizable column. */
function firstGrip(): HTMLElement {
  const grips = screen
    .getByTestId("grid-header")
    .querySelectorAll<HTMLElement>(".cursor-col-resize");
  expect(grips.length).toBeGreaterThan(0);
  return grips[0];
}

describe("EventGrid column sizing", () => {
  beforeEach(() => {
    useUiStore.setState({ columnWidths: {} });
  });

  it("sizes header cells from the table's own widths", () => {
    // `getSize()` comes from columnSizingFeature. Without it registered, this
    // would not be a number — it would not compile, but it would also silently
    // become `undefined` through any `any` on the path.
    renderGrid();
    const cells = screen
      .getByTestId("grid-header")
      .querySelectorAll<HTMLElement>("[data-column-drag]");
    const byId = Object.fromEntries(
      [...cells].map((c) => [c.getAttribute("data-column-drag"), c]),
    );
    // Fixed columns carry the table's measured width...
    expect(byId["timestamp"].style.flex).toMatch(/^0 0 \d+px$/);
    expect(byId["artifact"].style.flex).toMatch(/^0 0 \d+px$/);
    // ...and `message` absorbs the slack instead of taking one. (jsdom's CSS
    // parser drops the unitless `flex: 1 1 0` shorthand, so this asserts the
    // absence of a fixed width rather than the flex value itself.)
    expect(byId["message"].style.width).toBe("");
  });

  it("offers a resize grip on resizable columns", () => {
    renderGrid();
    expect(firstGrip()).toBeInTheDocument();
  });

  it("persists a width once the gesture ends, not on every pixel", () => {
    renderGrid();
    const setSpy = vi.spyOn(useUiStore.getState(), "setColumnWidth");

    fireEvent.mouseDown(firstGrip(), { clientX: 300 });
    fireEvent.mouseMove(document, { clientX: 360 });
    // Mid-drag: the width is live in the table but nothing is written yet —
    // this is the whole reason the effect keys on the resize state instead of
    // on the width.
    expect(setSpy).not.toHaveBeenCalled();

    fireEvent.mouseUp(document, { clientX: 360 });

    // Released: exactly one write, carrying the width the drag ended on. A
    // subscription to the wrong state slice leaves this at zero calls, which
    // is precisely the regression v9's rename invites.
    expect(setSpy).toHaveBeenCalledTimes(1);
    const [id, width] = setSpy.mock.calls[0];
    expect(typeof id).toBe("string");
    expect(width).toBeGreaterThan(0);
  });
});
