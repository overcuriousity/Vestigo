/**
 * A view block embeds up to `display.limit` rows (200 by default) into a
 * 320px scroller, so its preview is windowed rather than built in full on
 * every render of the story. The rows an analyst can see must still be the
 * real ones, and the count below the table must still describe the whole
 * embedded set — the export snapshot renders that set independently, so a
 * preview that quietly showed fewer rows than it claimed would be a
 * forensic-reporting problem, not just a cosmetic one.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ViewBlockCard } from "@/components/stories/EmbedCards";
import type { Event, StoryBlockOf } from "@/api/types";

// jsdom has no ResizeObserver, and react-virtual needs one that reports a
// non-zero height or it windows down to nothing (same stub pattern as
// chartProposalCard.test.tsx).
class FakeResizeObserver {
  private cb: ResizeObserverCallback;
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb;
  }
  observe(target: Element) {
    this.cb(
      [{ target, contentRect: { width: 600, height: 320 } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  // @ts-expect-error -- jsdom has no native ResizeObserver
  global.ResizeObserver = FakeResizeObserver;
  // The scroller's own box, which react-virtual reads directly on mount —
  // jsdom lays nothing out, so every rect is 0×0 and the window would be
  // empty for reasons that have nothing to do with the component.
  HTMLElement.prototype.getBoundingClientRect = () =>
    ({ width: 600, height: 320, top: 0, left: 0, right: 600, bottom: 320, x: 0, y: 0 }) as DOMRect;
  for (const [prop, value] of Object.entries({
    clientHeight: 320,
    offsetHeight: 320,
    clientWidth: 600,
    offsetWidth: 600,
  })) {
    Object.defineProperty(HTMLElement.prototype, prop, { configurable: true, value });
  }
});

const listViewsMock = vi.fn();
const listEventsMock = vi.fn();

vi.mock("@/api/views", () => ({ viewsApi: { list: (...a: unknown[]) => listViewsMock(...a) } }));
vi.mock("@/api/events", () => ({ eventsApi: { list: (...a: unknown[]) => listEventsMock(...a) } }));

const ROW_TOTAL = 200;

function events(n: number): Event[] {
  return Array.from({ length: n }, (_, i) => ({
    event_id: `e${i}`,
    source_id: "src1",
    timestamp: `2026-07-2${(i % 9) + 1}T10:00:00Z`,
    message: `row ${i}`,
    artifact: "log:line",
    attributes: {},
  })) as unknown as Event[];
}

function block(): StoryBlockOf<"view_ref"> {
  return {
    id: "b1",
    story_id: "s1",
    position: 1024,
    kind: "view_ref",
    content: { view_id: "v1", timeline_id: "t1", display: { limit: ROW_TOTAL } },
    origin: "user",
    version: 1,
    created_by: "alice",
    updated_by: "alice",
    created_at: "2026-07-26T12:00:00Z",
    updated_at: "2026-07-26T12:00:00Z",
  } as StoryBlockOf<"view_ref">;
}

beforeEach(() => {
  vi.clearAllMocks();
  listViewsMock.mockResolvedValue([{ id: "v1", name: "Failed logons", filter: {} }]);
  listEventsMock.mockResolvedValue({ events: events(ROW_TOTAL), total: 14203 });
});

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ViewBlockCard block={block()} caseId="c1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ViewBlockCard row preview", () => {
  it("renders the first rows without putting all of them in the DOM", async () => {
    renderCard();

    expect(await screen.findByText("row 0")).toBeInTheDocument();
    // Windowed: a 320px scroller at 22px per row holds ~15, plus overscan.
    // The assertion is deliberately loose — it fails on "all 200 rendered",
    // not on an off-by-one in the overscan.
    await waitFor(() => {
      const rendered = screen.queryAllByText(/^row \d+$/);
      expect(rendered.length).toBeGreaterThan(0);
      expect(rendered.length).toBeLessThan(ROW_TOTAL / 2);
    });
    expect(screen.queryByText(`row ${ROW_TOTAL - 1}`)).toBeNull();
  });

  it("still reports the full embedded set against the view's true total", async () => {
    renderCard();

    // 200 embedded of 14,203 matching — the number the export carries too.
    expect(await screen.findByText(/^200 of 14[.,]203 rows shown$/)).toBeInTheDocument();
  });
});
