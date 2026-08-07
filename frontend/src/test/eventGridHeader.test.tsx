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
