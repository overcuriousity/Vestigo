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
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoryEditor } from "@/components/stories/StoryEditor";
import type { StoryBlock } from "@/api/types";

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

/** React's own loop detector, which fires long before a test would time out. */
function loopErrors() {
  return consoleErrors.filter((e) => /Maximum update depth/.test(e));
}

beforeEach(() => {
  consoleErrors = [];
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
  return render(
    <QueryClientProvider client={qc}>
      <StoryEditor caseId="c1" storyId="s1" />
    </QueryClientProvider>,
  );
}

describe("StoryEditor render loop", () => {
  it("settles after rendering a story that contains a markdown block", async () => {
    renderEditor();
    await screen.findByText("hello");
    // The loop ran at roughly 700 updates/s, so a short settle window is
    // conclusive — pre-fix this saw 18 of these.
    await new Promise((r) => setTimeout(r, 300));

    expect(loopErrors()).toHaveLength(0);
  });

  it("still reports edit mode, without looping on the transition", async () => {
    renderEditor();
    fireEvent.click(await screen.findByText("hello"));

    // The effect that reports edit mode upward is what looped; it still has to
    // do its job, so editing must actually open.
    const textarea = await screen.findByRole("textbox");
    expect(textarea).toHaveValue("hello");

    fireEvent.keyDown(textarea, { key: "Escape" });
    await new Promise((r) => setTimeout(r, 200));
    expect(loopErrors()).toHaveLength(0);
  });
});
