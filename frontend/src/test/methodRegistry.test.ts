import { describe, expect, it } from "vitest";
import { buildParams } from "@/components/analysis/MethodKnobForm";
import {
  EVIDENCE_CLASSES,
  METHODS,
  METHODS_BY_ID,
  type MethodId,
} from "@/components/analysis/method-registry";

describe("method registry", () => {
  it("sends a numeric choice as a number, which the API's int Literal requires", () => {
    const meta = METHODS_BY_ID.time_of_day;
    expect(buildParams(meta, { bucket_minutes: "15", timezone: "UTC" }, {})).toEqual({
      bucket_minutes: 15,
      timezone: "UTC",
    });
    // A string choice stays a string.
    expect(buildParams(METHODS_BY_ID.entropy, { variant: "bigram" }, {})).toEqual({
      variant: "bigram",
    });
    for (const m of METHODS)
      for (const k of m.knobs)
        if (k.kind === "choice" && k.numeric)
          for (const o of k.options ?? []) expect(Number.isInteger(Number(o.value))).toBe(true);
  });

  it("gives every method an evidence class the rail can group by", () => {
    const known = new Set(EVIDENCE_CLASSES.map((c) => c.id));
    for (const m of METHODS) expect(known.has(m.evidenceClass)).toBe(true);
  });

  it("orders evidence classes strongest-claim first", () => {
    expect(EVIDENCE_CLASSES.map((c) => c.id)).toEqual(["named", "statistical", "exploration"]);
  });

  it("gives every method the prose the sheet renders in place of the Method tab", () => {
    for (const m of METHODS) {
      expect(m.what.length).toBeGreaterThan(40);
      expect(m.scoreUnit.length).toBeGreaterThan(0);
    }
  });

  it("tells a beginner when each method is worth configuring", () => {
    for (const m of METHODS) {
      expect(m.useWhen.startsWith("Use this when")).toBe(true);
      expect(m.useWhen.length).toBeGreaterThan(30);
      expect(m.useWhen.length).toBeLessThan(200);
    }
  });

  it("declares knobs only for params the backend accepts", () => {
    // Mirrors METHOD_PARAMS in api/routers/analysis.py. A knob the backend
    // rejects would 422 on rerun; the endpoint rejects unknown keys rather
    // than dropping them, precisely so this cannot fail silently.
    const backend: Record<MethodId, string[]> = {
      value_novelty: ["fields"],
      value_combo: ["fields"],
      numeric_range: ["fields"],
      charset: ["fields", "group_field"],
      entropy: ["fields", "variant"],
      frequency: ["series_field", "z_threshold"],
      proportion_shift: ["fields", "fdr_q", "min_ratio"],
      value_distribution_drift: ["fields", "fdr_q"],
      interval_periodicity: ["series_field", "fdr_q", "min_ratio"],
      timestamp_order: ["min_skew_seconds"],
      sequence_novelty: ["series_field", "ngram_size", "max_gap_seconds"],
      transition_time: ["series_field", "partition_field", "min_ratio"],
      time_of_day: ["fields", "bucket_minutes", "timezone"],
      value_correlation: ["fields", "fdr_q", "min_ratio", "rule_confidence", "min_support"],
      log_template: ["field", "order", "only_new"],
    };
    for (const m of METHODS) {
      for (const knob of m.knobs) {
        expect(backend[m.id]).toContain(knob.param);
      }
    }
  });

  it("covers exactly the fifteen methods the gate plans for", () => {
    // METHOD_IDS in db/analysis_plan.py. A method here that the plan never
    // reports would render with no status; one there that is missing here
    // would never be shown at all.
    expect([...METHODS].map((m) => m.id).sort()).toEqual(
      [
        "charset",
        "entropy",
        "frequency",
        "interval_periodicity",
        "log_template",
        "numeric_range",
        "proportion_shift",
        "sequence_novelty",
        "time_of_day",
        "timestamp_order",
        "transition_time",
        "value_combo",
        "value_correlation",
        "value_distribution_drift",
        "value_novelty",
      ].sort(),
    );
  });

  it("indexes every method by id", () => {
    expect(Object.keys(METHODS_BY_ID)).toHaveLength(METHODS.length);
  });
});

describe("query shapes", () => {
  it("gives every method one", () => {
    // The sheet renders this under "Query shape" for every method it can open.
    // A missing one renders an empty <pre> under a heading promising a query.
    for (const m of METHODS) {
      expect(m.querySketch.trim().length, `${m.id} has no querySketch`).toBeGreaterThan(0);
    }
  });

  it("names the events table and never claims to be a transcript", () => {
    // It is a teaching aid. The detectors do not return compiled SQL (Sigma
    // does), so a sketch that read like a captured statement would assert
    // something no code can be pointed at.
    for (const m of METHODS) {
      expect(m.querySketch, `${m.id}`).toMatch(/\bevents\b/);
    }
  });
});
