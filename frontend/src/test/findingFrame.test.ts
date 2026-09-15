import { describe, expect, it } from "vitest";
import {
  isSelfModeFinding,
  isTemporalMode,
  sliceWindowEnd,
} from "@/lib/finding-frame";
import type { FrequencyFinding, ProportionShiftFinding } from "@/api/types";

describe("finding-frame", () => {
  it("classifies every baseline-frame mode as temporal, including the four bare ones", () => {
    // `g-test`, `cadence`, `ngram` and `drift` never carried the `temporal-`
    // prefix, and a prefix test labelled all four as self-baseline.
    for (const m of ["temporal", "temporal-z-score", "temporal-bigram-iqr", "g-test", "cadence", "ngram", "drift"]) {
      expect(isTemporalMode(m)).toBe(true);
    }
    for (const m of ["self-baseline", "iqr", "bigram-iqr", "self-g-test", "self-cadence", "rare-ngram", "z-score"]) {
      expect(isTemporalMode(m)).toBe(false);
    }
  });

  it("offers a range highlight for slice findings the way frequency windows get one", () => {
    const shift = {
      type: "proportion_shift",
      details: { method: "self-g-test", window_end: "2026-02-02T03:00:00Z" },
    } as unknown as ProportionShiftFinding;
    expect(sliceWindowEnd(shift)).toBe("2026-02-02T03:00:00Z");
    expect(isSelfModeFinding(shift)).toBe(true);
    // A baseline-frame window already renders as a histogram band.
    const temporal = { ...shift, details: { method: "g-test", window_end: "x" } } as unknown as ProportionShiftFinding;
    expect(sliceWindowEnd(temporal)).toBeUndefined();
    expect(isSelfModeFinding(temporal)).toBe(false);
    const freq = { type: "frequency", window_end: "2026-02-02T04:00:00Z", details: {} } as unknown as FrequencyFinding;
    expect(sliceWindowEnd(freq)).toBe("2026-02-02T04:00:00Z");
  });
});
