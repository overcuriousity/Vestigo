/**
 * The markdown block must save against the version it started editing from,
 * not against whatever the 10s story poll last handed it.
 *
 * Reading the live prop meant a collaborator's save landing mid-edit silently
 * became the base version: the server's optimistic check then passed and their
 * edit was destroyed with no 409 and no conflict UI. A paragraph routinely
 * takes longer to write than the poll interval, so that was the common path.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarkdownBlock } from "@/components/stories/MarkdownBlock";
import type { StoryBlockOf } from "@/api/types";

function block(version: number, text: string): StoryBlockOf<"markdown"> {
  return {
    id: "b1",
    story_id: "s1",
    position: 1024,
    kind: "markdown",
    content: { text },
    origin: "user",
    version,
    created_by: "alice",
    updated_by: "alice",
    created_at: "2026-07-26T12:00:00Z",
    updated_at: "2026-07-26T12:00:00Z",
  };
}

function renderBlock(initial: StoryBlockOf<"markdown">, onSave = vi.fn()) {
  const view = render(
    <MarkdownBlock
      block={initial}
      onSave={onSave}
      conflict={null}
      onResolveConflict={() => {}}
      onEditingChange={() => {}}
    />,
  );
  const rerenderWith = (next: StoryBlockOf<"markdown">) =>
    view.rerender(
      <MarkdownBlock
        block={next}
        onSave={onSave}
        conflict={null}
        onResolveConflict={() => {}}
        onEditingChange={() => {}}
      />,
    );
  return { onSave, rerenderWith };
}

describe("MarkdownBlock optimistic version", () => {
  it("saves against the version captured at edit start, not the polled one", () => {
    const { onSave, rerenderWith } = renderBlock(block(1, "original"));

    fireEvent.click(screen.getByLabelText("Edit this text block"));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "my edit" } });

    // A collaborator saves while the textarea is open; the poll delivers it.
    rerenderWith(block(2, "their edit"));

    fireEvent.blur(screen.getByRole("textbox"));

    // Version 1 is stale on purpose — the server must reject this and the
    // editor must show the conflict, rather than overwriting version 2.
    expect(onSave).toHaveBeenCalledWith("my edit", 1);
  });

  it("does not save when the draft is unchanged from what was opened", () => {
    const { onSave } = renderBlock(block(3, "unchanged"));
    fireEvent.click(screen.getByLabelText("Edit this text block"));
    fireEvent.blur(screen.getByRole("textbox"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("compares the draft against the opened text, not the polled text", () => {
    // The collaborator's text arriving mid-edit must not make an untouched
    // draft look like a change (nor a real change look like none).
    const { onSave, rerenderWith } = renderBlock(block(1, "original"));
    fireEvent.click(screen.getByLabelText("Edit this text block"));
    rerenderWith(block(2, "their edit"));
    fireEvent.blur(screen.getByRole("textbox"));
    expect(onSave).not.toHaveBeenCalled();
  });
});
