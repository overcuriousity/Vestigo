/**
 * The block menu must not take the body pointer-events lock.
 *
 * A modal Radix layer locks input by setting `pointer-events: none` on
 * <body>, capturing whatever was there before and restoring it on unmount.
 * `BlockPicker`'s embed items ("Saved view"/"Saved chart"/"Event") open a
 * modal `Dialog` from inside `onSelect`, so with a modal menu the dialog's
 * layer mounted while the menu's lock was still up and captured `"none"` as
 * its own "original". The menu then unmounted and correctly restored `""`,
 * but closing the dialog restored what it had captured — leaving <body> at
 * `pointer-events: none` with no layer open at all. The page kept rendering
 * and polling and simply stopped accepting input until a reload, which is
 * why it read as a freeze rather than as a dead click target. Measured in
 * Chromium, before the fix:
 *
 *   menu open   -> bodyPE "none", menus 1, dialogs 0
 *   dialog open -> bodyPE ""    , menus 0, dialogs 1
 *   after abort -> bodyPE "none", menus 0, dialogs 0   <- stuck
 *
 * "Text" opens no dialog, so it never overlapped two layers — the one kind
 * of block that could still be inserted, which is what localised the bug.
 *
 * The invariant asserted here is the menu never locking the body, since that
 * is what makes the overlap impossible. Mounting the dialog itself is not
 * asserted in jsdom: Radix's dialog inside React 19 + RTL's async `act`
 * wrapper hangs for reasons unrelated to this bug, and the real-browser check
 * is what covered the full open/abort cycle.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BlockPicker } from "@/components/stories/BlockPicker";

vi.mock("@/api/views", () => ({ viewsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/viz", () => ({
  savedChartsApi: { list: vi.fn().mockResolvedValue({ charts: [] }) },
}));
vi.mock("@/api/timelines", () => ({
  timelinesApi: { list: vi.fn().mockResolvedValue([{ id: "t1", name: "All", is_default: true }]) },
}));

beforeEach(() => {
  document.body.style.pointerEvents = "";
});
afterEach(() => {
  document.body.style.pointerEvents = "";
});

function renderPicker() {
  const onInsert = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <BlockPicker caseId="c1" onInsert={onInsert} />
    </QueryClientProvider>,
  );
  return { onInsert };
}

function openMenu() {
  const trigger = screen.getByRole("button", { name: "Add block" });
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.click(trigger);
}

describe("BlockPicker keeps the page clickable", () => {
  it("does not lock body pointer-events while the menu is open", () => {
    renderPicker();
    openMenu();

    // The menu is genuinely open — otherwise this asserts nothing.
    expect(document.querySelectorAll('[role="menu"]')).toHaveLength(1);
    expect(screen.queryAllByText("Saved view")).toHaveLength(1);

    // The invariant. Pre-fix this was "none", and the dialog opening on top
    // of it inherited that as the value it would later restore.
    expect(document.body.style.pointerEvents).not.toBe("none");
    expect(getComputedStyle(document.body).pointerEvents).not.toBe("none");
  });

  it("leaves the body unlocked after inserting a text block", () => {
    const { onInsert } = renderPicker();
    openMenu();
    fireEvent.click(screen.queryAllByText("Text")[0]);

    expect(onInsert).toHaveBeenCalledWith("markdown", { text: "" });
    expect(document.body.style.pointerEvents).not.toBe("none");
  });
});
