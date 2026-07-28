/**
 * The story editor must not re-render itself in a loop (issue #193).
 *
 * `MarkdownBlock` reported edit mode through an effect that depended on the
 * callback's identity, and `StoryEditor` passed a fresh inline closure and
 * always returned a new `Set` from `setEditingIds` — so React could never bail
 * out. One markdown block was enough: ~700 updates/second, indefinitely, which
 * froze the whole story view. It did not need the reported "add a block" step;
 * opening any story that already contained a text block was enough.
 *
 * React reports this as "Maximum update depth exceeded" (a console error, not
 * a throw — which is why it presented as a hang rather than an error screen),
 * so that message is the assertion. There was no StoryEditor test at all before
 * this one; `markdownBlockVersion.test.tsx` renders the block in isolation with
 * a *stable* callback and so cannot see it.
 */
import { Profiler, useEffect, useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoryEditor } from "@/components/stories/StoryEditor";
import { nextEditingIds } from "@/components/stories/editingIds";
import { MarkdownBlock } from "@/components/stories/MarkdownBlock";
import type { StoryBlock, StoryBlockOf } from "@/api/types";

const getWithBlocksMock = vi.fn();

vi.mock("@/api/stories", async () => {
  const actual = await vi.importActual<typeof import("@/api/stories")>("@/api/stories");
  return {
    ...actual,
    storiesApi: { ...actual.storiesApi, getWithBlocks: (...a: unknown[]) => getWithBlocksMock(...a) },
  };
});

function markdownBlock(): StoryBlock {
  return {
    id: "b1",
    story_id: "s1",
    position: 1024,
    kind: "markdown",
    content: { text: "hello" },
    origin: "user",
    version: 1,
    created_by: "alice",
    updated_by: "alice",
    created_at: "2026-07-26T12:00:00Z",
    updated_at: "2026-07-26T12:00:00Z",
  };
}

let consoleErrors: string[] = [];
let errorSpy: ReturnType<typeof vi.spyOn>;
let commits = 0;

/**
 * Renders of the editor subtree, counted by React itself.
 *
 * The budget is generous — mount plus the query resolving plus a couple of
 * effects is well under it — so a partial regression (one of the two guards
 * reverted) trips it in well under a second instead of waiting for React's
 * own "Maximum update depth" to get loud.
 *
 * It cannot rescue the *both*-guards-reverted case, and neither can the
 * per-test timeout: that loop is synchronous, so it starves the event loop
 * and no timer — vitest's included — ever fires. Measured: the suite hangs
 * until the runner is killed. That is not a gap in the test so much as the
 * reason both guards exist; each one alone keeps the failure reportable.
 */
const RENDER_BUDGET = 25;
function renderCount() {
  return commits;
}

/** React's own loop detector, which fires long before a test would time out. */
function loopErrors() {
  return consoleErrors.filter((e) => /Maximum update depth/.test(e));
}

beforeEach(() => {
  consoleErrors = [];
  commits = 0;
  errorSpy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    consoleErrors.push(String(args[0]));
  });
  getWithBlocksMock.mockResolvedValue({
    id: "s1",
    case_id: "c1",
    title: "Story",
    blocks: [markdownBlock()],
  });
});

afterEach(() => errorSpy.mockRestore());

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={qc}>
      <Profiler id="editor" onRender={() => (commits += 1)}>
        <StoryEditor caseId="c1" storyId="s1" />
      </Profiler>
    </QueryClientProvider>,
  );
  return { ...result, qc };
}

// Every test here carries an explicit timeout, so a regression that still
// yields to the event loop fails quickly and says why, rather than sitting
// until the suite-level default. See RENDER_BUDGET for the one case no
// timeout can catch.
const TEST_TIMEOUT = 5_000;

describe("StoryEditor render loop", () => {
  it(
    "settles after rendering a story that contains a markdown block",
    async () => {
      renderEditor();
      await screen.findByText("hello");
      // The loop ran at roughly 700 updates/s, so a short settle window is
      // conclusive — pre-fix this saw 18 of these.
      await new Promise((r) => setTimeout(r, 300));

      expect(loopErrors()).toHaveLength(0);
      // React's own signal only fires at ~50 nested updates, so the render
      // count is the tighter of the two and names the defect directly.
      expect(renderCount()).toBeLessThan(RENDER_BUDGET);
    },
    TEST_TIMEOUT,
  );

  it(
    "still reports edit mode, without looping on the transition",
    async () => {
      renderEditor();
      fireEvent.click(await screen.findByText("hello"));

      // The effect that reports edit mode upward is what looped; it still has
      // to do its job, so editing must actually open.
      const textarea = await screen.findByRole("textbox");
      expect(textarea).toHaveValue("hello");

      fireEvent.keyDown(textarea, { key: "Escape" });
      await new Promise((r) => setTimeout(r, 200));
      expect(loopErrors()).toHaveLength(0);
      expect(renderCount()).toBeLessThan(RENDER_BUDGET);
    },
    TEST_TIMEOUT,
  );

  it(
    "clears edit state when a block goes away while being edited",
    async () => {
      // A collaborator deleting the block an analyst is editing unmounts the
      // MarkdownBlock while the editor stays. Without the effect's cleanup its
      // id stays in editingIds forever, and the "your draft is kept" banner
      // outlives the block it describes.
      const { qc } = renderEditor();
      fireEvent.click(await screen.findByText("hello"));
      await screen.findByRole("textbox");
      expect(screen.getByText(/your draft is kept/)).toBeInTheDocument();

      getWithBlocksMock.mockResolvedValue({
        id: "s1",
        case_id: "c1",
        title: "Story",
        blocks: [],
      });
      await qc.invalidateQueries({ queryKey: ["story", "c1", "s1"] });

      await waitFor(() => expect(screen.queryByText(/your draft is kept/)).toBeNull());
    },
    TEST_TIMEOUT,
  );
});

// Each half of the fix, pinned on its own. Either one alone stops the loop, so
// the tests above stay green if one is reverted — which is exactly the
// redundancy the fix was written for.
describe("StoryEditor render loop — each guard independently", () => {
  it(
    "MarkdownBlock does not re-run its effect when only the callback identity changes",
    async () => {
      // The MarkdownBlock half: a parent that re-renders with a fresh closure
      // every time must not make the block report edit mode again.
      const reports: boolean[] = [];
      const PARENT_RENDERS = 5;
      function Parent() {
        const [n, setN] = useState(0);
        useEffect(() => {
          if (n >= PARENT_RENDERS) return;
          const id = setTimeout(() => setN(n + 1), 0);
          return () => clearTimeout(id);
        }, [n]);
        return (
          <MarkdownBlock
            block={markdownBlock() as StoryBlockOf<"markdown">}
            conflict={null}
            // A brand-new closure on every parent render, which is exactly
            // what StoryEditor passes.
            onEditingChange={(editing) => reports.push(editing)}
            onSave={() => {}}
            onResolveConflict={() => {}}
          />
        );
      }
      render(<Parent />);
      await screen.findByText("hello");
      await waitFor(() => expect(reports.length).toBeGreaterThan(0));
      await new Promise((r) => setTimeout(r, 200));

      // One report for the mount, and no more — not one per parent render,
      // which is what depending on the callback's identity produced.
      expect(reports).toEqual([false]);
    },
    TEST_TIMEOUT,
  );

  it("nextEditingIds returns the same Set when membership does not change", () => {
    // The StoryEditor half, asserted on the real updater's contract: React
    // bails out on Object.is, so reporting the same state twice must hand back
    // the identical Set.
    const empty = new Set<string>();
    expect(nextEditingIds(empty, "b1", false)).toBe(empty);

    const editing = nextEditingIds(empty, "b1", true);
    expect(editing).not.toBe(empty);
    expect([...editing]).toEqual(["b1"]);
    expect(nextEditingIds(editing, "b1", true)).toBe(editing);

    const done = nextEditingIds(editing, "b1", false);
    expect(done).not.toBe(editing);
    expect([...done]).toEqual([]);
  });
});
