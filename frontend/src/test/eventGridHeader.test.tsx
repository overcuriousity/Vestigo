/**
 * The event grid's header must live inside the scroll element, so horizontal
 * scrolling moves it with the columns, and both header and rows must span the
 * content width rather than the viewport width.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { EventGrid } from "@/components/explorer/EventGrid";
import { reorderColumns } from "@/lib/columns";
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

function renderGrid(visibleColumns: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
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
        visibleColumns={visibleColumns}
        sortDir="desc"
        onSortToggle={vi.fn()}
      />
    </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("EventGrid scroll structure", () => {
  it("nests the header inside the scroll element", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const scroller = screen.getByTestId("grid-scroll");
    expect(scroller).toContainElement(screen.getByTestId("grid-header"));
  });

  it("sticks the header to the top of the scroller", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    expect(screen.getByTestId("grid-header").className).toContain("sticky");
  });

  it("sizes the shared wrapper to the content width, not the viewport", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const wrapper = screen.getByTestId("grid-content");
    expect(wrapper.style.minWidth).toMatch(/^\d+px$/);
  });

  it("puts header and body in the same width-constrained wrapper", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const wrapper = screen.getByTestId("grid-content");
    expect(wrapper).toContainElement(screen.getByTestId("grid-header"));
    expect(wrapper).toContainElement(screen.getByTestId("grid-body"));
  });
});

describe("EventGrid column reorder", () => {
  it("makes each visible column header a drag handle", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const header = screen.getByTestId("grid-header");
    expect(header.querySelectorAll("[data-column-drag]")).toHaveLength(3);
  });

  it("does not make the grid-internal columns draggable", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const header = screen.getByTestId("grid-header");
    const ids = [...header.querySelectorAll("[data-column-drag]")].map((el) =>
      el.getAttribute("data-column-drag"),
    );
    expect(ids).toEqual(["timestamp", "artifact", "message"]);
    expect(ids).not.toContain("_select");
    expect(ids).not.toContain("_annotations");
    expect(ids).not.toContain("_expand");
  });
});

/**
 * A jsdom test cannot faithfully simulate a dnd-kit pointer drag, so the
 * ordering logic lives in a pure function and is tested here directly —
 * asserting on a simulated drag would be testing dnd-kit, not this code.
 */
describe("reorderColumns", () => {
  it("moves the dragged column to the target's position", () => {
    expect(reorderColumns(["a", "b", "c"], "a", "c")).toEqual(["b", "c", "a"]);
    expect(reorderColumns(["a", "b", "c"], "c", "a")).toEqual(["c", "a", "b"]);
  });

  it("returns the input unchanged when the ids are the same", () => {
    const cols = ["a", "b", "c"];
    expect(reorderColumns(cols, "b", "b")).toBe(cols);
  });

  it("returns the input unchanged when either id is not a visible column", () => {
    const cols = ["a", "b", "c"];
    expect(reorderColumns(cols, "a", "_expand")).toBe(cols);
    expect(reorderColumns(cols, "zz", "b")).toBe(cols);
  });
});
