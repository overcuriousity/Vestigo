import { describe, expect, it } from "vitest";
import { afterIdForIndex, sortBlocks } from "@/components/stories/blockOrder";
import type { StoryBlock } from "@/api/types";

const b = (id: string, position: number) => ({ id, position }) as StoryBlock;

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
