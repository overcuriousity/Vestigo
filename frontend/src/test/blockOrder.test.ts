import { describe, expect, it } from "vitest";
import { afterIdForIndex, reorderLocally, sortBlocks } from "@/components/stories/blockOrder";
import type { StoryBlock } from "@/api/types";

const b = (id: string, position: number) => ({ id, position }) as StoryBlock;
const ids = (blocks: StoryBlock[]) => blocks.map((x) => x.id);

describe("sortBlocks", () => {
  it("orders by position ascending without mutating the input", () => {
    const input = [b("z", 3072), b("a", 1024), b("m", 2048)];
    expect(sortBlocks(input).map((x) => x.id)).toEqual(["a", "m", "z"]);
    expect(input[0].id).toBe("z");
  });
});

describe("afterIdForIndex", () => {
  const sorted = [b("a", 1024), b("m", 2048), b("z", 3072)];

  it("index 0 means top of document", () => {
    expect(afterIdForIndex(sorted, 0, "z")).toBeNull();
  });

  it("anchors on the block above the target slot", () => {
    expect(afterIdForIndex(sorted, 1, "z")).toBe("a");
  });

  it("skips the moving block itself when counting", () => {
    expect(afterIdForIndex(sorted, 2, "m")).toBe("z");
  });

  it("clamps an out-of-range index to the end", () => {
    expect(afterIdForIndex(sorted, 99, "a")).toBe("z");
  });
});

describe("reorderLocally", () => {
  const blocks = [b("b1", 1024), b("b2", 2048), b("b3", 3072)];

  it("moves to the top on a null anchor", () => {
    expect(ids(reorderLocally(blocks, "b3", null))).toEqual(["b3", "b1", "b2"]);
  });

  it("places the block directly after its anchor", () => {
    expect(ids(reorderLocally(blocks, "b1", "b2"))).toEqual(["b2", "b1", "b3"]);
    expect(ids(reorderLocally(blocks, "b1", "b3"))).toEqual(["b2", "b3", "b1"]);
  });

  it("is a no-op when the block is already there", () => {
    expect(ids(reorderLocally(blocks, "b2", "b1"))).toEqual(["b1", "b2", "b3"]);
  });

  it("leaves the list alone for an unknown block or anchor", () => {
    expect(ids(reorderLocally(blocks, "ghost", null))).toEqual(["b1", "b2", "b3"]);
    expect(ids(reorderLocally(blocks, "b1", "ghost"))).toEqual(["b1", "b2", "b3"]);
  });

  it("agrees with afterIdForIndex, so the optimistic order matches the server's", () => {
    // The two are used together on every drag: afterIdForIndex derives what the
    // API is told, reorderLocally predicts what it will return. A disagreement
    // makes the dragged block visibly jump twice.
    for (let target = 0; target < blocks.length; target++) {
      for (const moving of blocks) {
        const anchor = afterIdForIndex(blocks, target, moving.id);
        const local = ids(reorderLocally(blocks, moving.id, anchor));
        const expected = ids(blocks).filter((id) => id !== moving.id);
        expected.splice(anchor === null ? 0 : expected.indexOf(anchor) + 1, 0, moving.id);
        expect(local).toEqual(expected);
      }
    }
  });
});
