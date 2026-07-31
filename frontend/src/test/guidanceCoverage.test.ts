/**
 * `GuidancePanel` takes an id and nothing else, so the forward direction — a
 * panel whose copy was inlined instead of registered — is a compile error and
 * needs no test.
 *
 * This covers the direction the type system cannot see: an entry in the registry
 * that nothing renders. `vizExplainers.test.ts` makes the same argument for the
 * explainer copy, and notes that this is the direction that actually bites — the
 * σ statistic shipped with no explainer beside it while mean/median/skewness all
 * had one. Unreferenced guidance means either a panel was removed and its copy
 * left behind, or copy was written for a panel that never got mounted.
 */
import { describe, it, expect } from "vitest";
import { guidance } from "@/lib/guidance";

// Raw glob rather than node:fs — the frontend tsconfig carries no node types.
// Same constraint and same solution as vizExplainers.test.ts.
const SOURCES = {
  ...(import.meta.glob("../components/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  ...(import.meta.glob("../pages/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
};

function renderedGuidanceIds(): Set<string> {
  const used = new Set<string>();
  for (const source of Object.values(SOURCES)) {
    for (const match of source.matchAll(/<GuidancePanel\s+id="([^"]+)"/g)) {
      used.add(match[1]);
    }
  }
  return used;
}

describe("guidance registry", () => {
  it("renders every entry it defines", () => {
    const rendered = renderedGuidanceIds();
    expect(rendered.size).toBeGreaterThan(0);
    for (const id of Object.keys(guidance)) {
      expect(rendered.has(id), `guidance["${id}"] has copy but no panel shows it`).toBe(true);
    }
  });

  it("gives every entry a title and a body", () => {
    for (const [id, entry] of Object.entries(guidance)) {
      expect(entry.title, id).toBeTruthy();
      expect(entry.body, id).toBeTruthy();
    }
  });
});
