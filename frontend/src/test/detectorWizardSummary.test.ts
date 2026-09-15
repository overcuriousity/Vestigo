import { describe, expect, it } from "vitest";
import { METHODS_BY_ID } from "@/components/analysis/method-registry";
import { summarize } from "@/components/analysis/detector-wizard-summary";

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

  it("reads the comparison methods on the self frame like any other method", () => {
    // A baseline never restricts a detector (D18): proportion shift without
    // one takes its reference from the timeline itself.
    expect(summarize(METHODS_BY_ID.proportion_shift, { fdr_q: 0.01 }, "self", null)).toBe(
      "Proportion shift over fields Vestigo picks, fdr q 0.01, across the whole timeline. Full scan.",
    );
  });
});
