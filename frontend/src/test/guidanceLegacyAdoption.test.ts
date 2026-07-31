/**
 * Adoption of the pre-v5 `vestigo-guidance-<id>` localStorage keys.
 *
 * The interesting case is the one `migrate` cannot reach: a browser that folded
 * a guidance panel away but never touched a UI preference has legacy keys and no
 * `vestigo-ui` entry at all, so no migration ever runs for it. Doing the adoption
 * in `onRehydrateStorage` covers that browser and guarantees the cleanup
 * completes, which matters because otherwise those keys outlive every future
 * upgrade — the next preference write persists at v5 directly.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { useUiStore as UiStore } from "@/stores/ui";

/** A store instance that has just rehydrated from whatever localStorage holds. */
async function freshStore(): Promise<typeof UiStore> {
  vi.resetModules();
  return (await import("@/stores/ui")).useUiStore;
}

const LEGACY = "vestigo-guidance-cases-page";

describe("legacy guidance key adoption", () => {
  beforeEach(() => localStorage.clear());

  it("adopts a dismissal from a browser that never persisted a UI preference", async () => {
    localStorage.setItem(LEGACY, "collapsed");

    const store = await freshStore();

    expect(store.getState().collapsedGuidance["cases-page"]).toBe(true);
    expect(localStorage.getItem(LEGACY)).toBeNull();
  });

  it("clears a legacy key even when its value was never a dismissal", async () => {
    localStorage.setItem(LEGACY, "expanded");

    const store = await freshStore();

    expect(store.getState().collapsedGuidance["cases-page"]).toBeUndefined();
    expect(localStorage.getItem(LEGACY)).toBeNull();
  });

  it("lets the store's own state win over a stale legacy key", async () => {
    localStorage.setItem(LEGACY, "collapsed");
    localStorage.setItem(
      "vestigo-ui",
      JSON.stringify({ version: 5, state: { collapsedGuidance: { "cases-page": false } } }),
    );

    const store = await freshStore();

    // Re-expanded after the legacy key was written, so the dismissal is stale.
    expect(store.getState().collapsedGuidance["cases-page"]).toBe(false);
    expect(localStorage.getItem(LEGACY)).toBeNull();
  });

  it("carries a v4 store's dismissals forward through the migration", async () => {
    localStorage.setItem(LEGACY, "collapsed");
    localStorage.setItem(
      "vestigo-ui",
      JSON.stringify({ version: 4, state: { density: "compact" } }),
    );

    const store = await freshStore();

    expect(store.getState().collapsedGuidance["cases-page"]).toBe(true);
    expect(store.getState().density).toBe("compact");
  });
});
