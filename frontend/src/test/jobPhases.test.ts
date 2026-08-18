import { describe, it, expect } from "vitest";
import { jobOutcomeNote, jobPhaseLabel } from "@/lib/jobPhases";

describe("jobPhaseLabel", () => {
  it("reads the same phase token in opposite directions per job kind", () => {
    // The whole reason the map is keyed on `kind`: "events" means packing on
    // the way out and restoring on the way in.
    expect(jobPhaseLabel("case_export", { phase: "events" })).toBe("Packing events");
    expect(jobPhaseLabel("case_import", { phase: "events" })).toBe("Restoring events");
    expect(jobPhaseLabel("case_export", { phase: "postgres" })).toBe("Collecting case records");
    expect(jobPhaseLabel("case_import", { phase: "postgres" })).toBe("Restoring case records");
  });

  it("covers every phase the backend emits", () => {
    for (const phase of ["queued", "postgres", "events", "blobs", "manifest"]) {
      expect(jobPhaseLabel("case_export", { phase })).toBeTruthy();
    }
    for (const phase of ["queued", "verify", "postgres", "events", "blobs", "stats"]) {
      expect(jobPhaseLabel("case_import", { phase })).toBeTruthy();
    }
  });

  it("never leaks a raw token for an unknown phase or kind", () => {
    expect(jobPhaseLabel("case_export", { phase: "verify" })).toBeNull();
    expect(jobPhaseLabel("case_import", { phase: "manifest" })).toBeNull();
    expect(jobPhaseLabel("case_export", { phase: "brand_new_phase" })).toBeNull();
    expect(jobPhaseLabel("ingest", { phase: "events" })).toBeNull();
    expect(jobPhaseLabel(undefined, { phase: "events" })).toBeNull();
  });

  it("returns null when there is no phase to describe", () => {
    expect(jobPhaseLabel("case_import", null)).toBeNull();
    expect(jobPhaseLabel("case_import", { total: 5, processed: 1 })).toBeNull();
  });
});

describe("jobOutcomeNote", () => {
  it("names a duplicate outcome of an AI conversion", () => {
    expect(jobOutcomeNote("convert_ingest", { source_id: "s", duplicate: true })).toMatch(
      /already has/,
    );
  });

  it("is silent for ordinary completions and other kinds", () => {
    expect(jobOutcomeNote("convert_ingest", { source_id: "s" })).toBeNull();
    expect(jobOutcomeNote("convert_ingest", null)).toBeNull();
    expect(jobOutcomeNote("ingest", { duplicate: true })).toBeNull();
    expect(jobOutcomeNote(undefined, undefined)).toBeNull();
  });
});
