import { describe, expect, it } from "vitest";
import { METHODS_BY_ID } from "@/components/analysis/method-registry";
import { NEEDS_BASELINE, summarize } from "@/components/analysis/detector-wizard-summary";

describe("detector wizard summary", () => {
  it("names the method, the fields and the scope in one sentence", () => {
    expect(
      summarize(METHODS_BY_ID.value_novelty, { fields: ["user", "process"] }, "baseline", "week before"),
    ).toBe("Rare values over user and process, comparing to baseline “week before”. Cheap scan.");
  });

  it("says auto when the method picks its own fields", () => {
    expect(summarize(METHODS_BY_ID.charset, {}, "self", null)).toBe(
      "Charset novelty over fields Vestigo picks, across the whole timeline. Full scan.",
    );
  });

  it("names a series field and a threshold when set", () => {
    expect(
      summarize(METHODS_BY_ID.frequency, { series_field: "attr:host", z_threshold: 3 }, "self", null),
    ).toBe("Frequency per attr:host, |z| ≥ 3, across the whole timeline. Full scan.");
  });

  it("knows which methods cannot run without a baseline", () => {
    expect([...NEEDS_BASELINE].sort()).toEqual([
      "interval_periodicity",
      "proportion_shift",
      "sequence_novelty",
      "value_distribution_drift",
    ]);
  });
});
