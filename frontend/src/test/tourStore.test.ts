import { beforeEach, describe, expect, it } from "vitest";
import { TOUR_STEPS } from "@/lib/tourSteps";
import { tourEvent, useTourStore } from "@/stores/tour";

function step() {
  return TOUR_STEPS[useTourStore.getState().stepIndex];
}

describe("tour store", () => {
  beforeEach(() => {
    useTourStore.getState().stop();
  });

  it("starts idle and activates at step 0", () => {
    expect(useTourStore.getState().status).toBe("idle");
    useTourStore.getState().start();
    expect(useTourStore.getState().status).toBe("active");
    expect(useTourStore.getState().stepIndex).toBe(0);
  });

  it("ignores events while idle", () => {
    tourEvent("case-created");
    expect(useTourStore.getState().status).toBe("idle");
    expect(useTourStore.getState().stepIndex).toBe(0);
  });

  it("advances only on the event the current step waits for", () => {
    useTourStore.getState().start();
    expect(step().id).toBe("create-case");
    tourEvent("filter-added"); // wrong event — no advance
    expect(step().id).toBe("create-case");
    tourEvent("case-created");
    expect(step().id).toBe("open-case");
  });

  it("advances route-gated steps on a matching route change only", () => {
    useTourStore.getState().start();
    tourEvent("case-created"); // -> open-case (advance: route /cases/:caseId)
    useTourStore.getState().handleRouteChange("/settings");
    expect(step().id).toBe("open-case");
    useTourStore.getState().handleRouteChange("/cases/abc123");
    expect(step().id).toBe("converter-hint");
  });

  it("walks the full step sequence and finishes", () => {
    const s = useTourStore.getState();
    s.start();
    tourEvent("case-created");
    s.handleRouteChange("/cases/c1");
    useTourStore.getState().next(); // converter-hint (manual)
    tourEvent("upload-dialog-opened");
    tourEvent("source-uploaded");
    expect(step().id).toBe("ingesting");
    tourEvent("ingest-complete");
    expect(step().id).toBe("all-sources");
    s.handleRouteChange("/cases/c1/timelines/t1");
    useTourStore.getState().next(); // columns (manual)
    tourEvent("event-expanded");
    tourEvent("filter-added");
    s.handleRouteChange("/cases/c1/timelines/t1/visualize");
    expect(step().id).toBe("done");
    useTourStore.getState().next(); // Finish
    expect(useTourStore.getState().status).toBe("finished");
  });

  it("skip finishes from any step", () => {
    useTourStore.getState().start();
    useTourStore.getState().skip();
    expect(useTourStore.getState().status).toBe("finished");
  });

  it("back does not go below step 0", () => {
    useTourStore.getState().start();
    useTourStore.getState().back();
    expect(useTourStore.getState().stepIndex).toBe(0);
  });

  it("every step selector uses the data-tour convention", () => {
    for (const s of TOUR_STEPS) {
      if (!s.selector) continue;
      const selectors = Array.isArray(s.selector) ? s.selector : [s.selector];
      for (const sel of selectors) {
        expect(sel).toMatch(/^\[data-tour="[a-z-]+"\]/);
      }
    }
  });
});
