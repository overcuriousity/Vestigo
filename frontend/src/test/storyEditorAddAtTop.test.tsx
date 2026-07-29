/**
 * "Add at top" must add at the top.
 *
 * It sent `after_block_id: null`, which the create endpoint reads as "append
 * at end" — the value only means "top of document" on the *move* endpoint
 * (docs/STORIES.md). So the button whose label promised the top put the block
 * at the bottom. The create API now takes an explicit `at_top`, and this pins
 * which inserter sends it: the top one, and only the top one.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StoryEditor } from "@/components/stories/StoryEditor";
import type { StoryBlock } from "@/api/types";

const getWithBlocksMock = vi.fn();
const createBlockMock = vi.fn();

vi.mock("@/api/stories", async () => {
  const actual = await vi.importActual<typeof import("@/api/stories")>("@/api/stories");
  return {
    ...actual,
    storiesApi: {
      ...actual.storiesApi,
      getWithBlocks: (...a: unknown[]) => getWithBlocksMock(...a),
      createBlock: (...a: unknown[]) => createBlockMock(...a),
    },
  };
});

function block(id: string, position: number, text: string): StoryBlock {
  return {
    id,
    story_id: "s1",
    position,
    kind: "markdown",
    content: { text },
    origin: "user",
    version: 1,
    created_by: "admin",
    updated_by: "admin",
    created_at: "2026-07-29T12:00:00Z",
    updated_at: "2026-07-29T12:00:00Z",
  };
}

beforeEach(() => {
  // Not configured globally, and both tests read `mock.calls[0]`.
  getWithBlocksMock.mockClear();
  createBlockMock.mockClear();
  getWithBlocksMock.mockResolvedValue({
    id: "s1",
    case_id: "c1",
    title: "Story",
    blocks: [block("b1", 1024, "first"), block("b2", 2048, "second")],
  });
  createBlockMock.mockResolvedValue(block("b3", 512, ""));
});

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <StoryEditor caseId="c1" storyId="s1" />
    </QueryClientProvider>,
  );
}

/** Open one inserter's menu and choose "Text". */
function insertTextVia(trigger: HTMLElement) {
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.click(trigger);
  fireEvent.click(screen.queryAllByText("Text")[0]);
}

describe('StoryEditor "Add at top"', () => {
  it("asks the API for the top, not for an append", async () => {
    renderEditor();
    await screen.findByText("first");

    insertTextVia(screen.getByRole("button", { name: "Add at top" }));

    await waitFor(() => expect(createBlockMock).toHaveBeenCalled());
    const body = createBlockMock.mock.calls[0][2];
    expect(body.at_top).toBe(true);
    // Sending both is a 422 — the top inserter must not also name an anchor.
    expect(body.after_block_id ?? null).toBeNull();
  });

  it("still anchors a between-blocks insert to the block above it", async () => {
    renderEditor();
    await screen.findByText("first");

    // [0] is "Add at top"; the next one follows the first block.
    insertTextVia(screen.getAllByRole("button", { name: "Add block" })[0]);

    await waitFor(() => expect(createBlockMock).toHaveBeenCalled());
    const body = createBlockMock.mock.calls[0][2];
    expect(body.after_block_id).toBe("b1");
    expect(body.at_top).toBeUndefined();
  });
});
