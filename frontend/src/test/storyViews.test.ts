/**
 * Pushing live Explorer filters into a story must reuse the View that already
 * encodes them. Creating one unconditionally left a pile of identically-named
 * Views behind — one per push of the same filters to the same story.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EventFilters, View } from "@/api/types";

const listMock = vi.fn();
const createMock = vi.fn();

vi.mock("@/api/views", () => ({
  viewsApi: {
    list: (...args: unknown[]) => listMock(...args),
    create: (...args: unknown[]) => createMock(...args),
  },
}));

const { findOrCreateView, viewMatchesFilters } = await import("@/lib/storyViews");

function view(overrides: Partial<View>): View {
  return {
    id: "v1",
    case_id: "c1",
    name: "SSH hits",
    query: "ssh",
    filter: { q: "ssh", artifacts: ["linux:syslog"] },
    created_at: "2026-07-26T12:00:00Z",
    ...overrides,
  } as View;
}

const FILTERS: EventFilters = { q: "ssh", artifacts: ["linux:syslog"] };

beforeEach(() => {
  vi.clearAllMocks();
  createMock.mockResolvedValue(view({ id: "new" }));
});

describe("findOrCreateView", () => {
  it("reuses a View that already encodes these filters", async () => {
    listMock.mockResolvedValue([view({ id: "existing" })]);
    const result = await findOrCreateView("c1", "Report — view", FILTERS);
    expect(result.id).toBe("existing");
    expect(createMock).not.toHaveBeenCalled();
  });

  it("creates one when no saved View matches", async () => {
    listMock.mockResolvedValue([view({ id: "other", query: "sudo", filter: { q: "sudo" } })]);
    const result = await findOrCreateView("c1", "Report — view", FILTERS);
    expect(result.id).toBe("new");
    expect(createMock).toHaveBeenCalledWith("c1", "Report — view", "ssh", expect.any(Object));
  });

  it("does not reuse a View whose filters merely overlap", async () => {
    listMock.mockResolvedValue([
      view({ id: "narrower", filter: { q: "ssh", artifacts: ["linux:syslog"], sourceId: "s1" } }),
    ]);
    const result = await findOrCreateView("c1", "Report — view", FILTERS);
    expect(result.id).toBe("new");
  });
});

describe("viewMatchesFilters", () => {
  it("ignores key order and empty selections", () => {
    expect(
      viewMatchesFilters(
        view({ filter: { artifacts: ["linux:syslog"], q: "ssh", filters: {}, tagsInclude: [] } }),
        FILTERS,
      ),
    ).toBe(true);
  });

  it("distinguishes a different query", () => {
    expect(viewMatchesFilters(view({ query: "sudo" }), FILTERS)).toBe(false);
  });
});
