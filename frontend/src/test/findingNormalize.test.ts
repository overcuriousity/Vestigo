/**
 * finding-normalize: the unified feed's shape-flattening and the per-detector
 * rank interleave. The switch must cover every member of the AnomalyFinding
 * union (TS enforces exhaustiveness) — these tests pin the observable output
 * for representative shapes.
 */
import { describe, expect, it } from "vitest";
import { interleaveByRank, normalizeFinding, type FeedItem } from "@/lib/finding-normalize";
import { DETECTORS_BY_ID } from "@/components/analysis/detector-registry";
import type {
  DistributionDriftFinding,
  FrequencyFinding,
  IntervalPeriodicityFinding,
  ProportionShiftFinding,
  SequenceNoveltyFinding,
  TimestampOrderFinding,
  ValueNoveltyFinding,
} from "@/api/types";

const base = { event: null, details: {} as Record<string, unknown> };

function valueNovelty(value: string, score = 5): ValueNoveltyFinding {
  return {
    ...base,
    type: "value_novelty",
    field: "artifact",
    value,
    count: 2,
    score,
    first_seen: "2024-01-01T00:00:00Z",
    event_id: "e1",
  };
}

describe("normalizeFinding", () => {
  it("flattens a value_novelty finding", () => {
    const item = normalizeFinding(DETECTORS_BY_ID.novelty, valueNovelty("ssh_login"), 0);
    expect(item.detector).toBe("value_novelty");
    expect(item.title).toContain("ssh_login");
    expect(item.scoreRaw).toBe(5);
    expect(item.scoreUnit).toBe("surprise");
    expect(item.eventId).toBe("e1");
    expect(item.ts).toBe("2024-01-01T00:00:00Z");
    expect(item.rank).toBe(0);
  });

  it("flattens a frequency finding with the window timestamp fallback", () => {
    const f: FrequencyFinding = {
      ...base,
      type: "frequency",
      series_field: "artifact",
      series_value: "dns",
      window_start: "2024-01-02T00:00:00Z",
      window_end: "2024-01-02T01:00:00Z",
      observed: 900,
      expected: 100,
      z_score: 8,
      score: 8,
      event_id: null,
    };
    const item = normalizeFinding(DETECTORS_BY_ID.frequency, f, 3);
    expect(item.subtitle).toContain("900 observed");
    expect(item.ts).toBe("2024-01-02T00:00:00Z");
    expect(item.scoreUnit).toBe("|z|");
    expect(item.rank).toBe(3);
  });

  it("flattens a timestamp_order finding (no value key)", () => {
    const f: TimestampOrderFinding = {
      ...base,
      type: "timestamp_order",
      source_id: "s1",
      event_id: "e9",
      timestamp: "2024-01-03T00:00:00Z",
      prev_timestamp: "2024-01-03T00:10:00Z",
      skew_seconds: 600,
      byte_offset: 10,
      line_number: 2,
      score: 600,
    };
    const item = normalizeFinding(DETECTORS_BY_ID.order, f, 0);
    // The headline is the record's position, not the source id: a real one is
    // ~60 characters whose first 24 repeat across every source in the case, so
    // it wrapped over three lines and buried the skew. The source moves to the
    // subtitle, shortened to its distinguishing tail.
    expect(item.title).toContain("line 2");
    expect(item.subtitle).toContain("600.0s");
    expect(item.subtitle).toContain("s1");
    expect(item.ts).toBe("2024-01-03T00:00:00Z");
  });

  it("flattens a sequence_novelty finding with the arrow-joined value", () => {
    const f: SequenceNoveltyFinding = {
      ...base,
      type: "sequence_novelty",
      field: "artifact",
      values: ["a", "b", "c"],
      value: "a → b → c",
      count: 2,
      score: 6.9,
      first_seen: "2024-01-04T00:00:00Z",
      event_id: "e2",
      details: { window_label: "incident" },
    };
    const item = normalizeFinding(DETECTORS_BY_ID.sequence, f, 1);
    expect(item.title).toContain("a → b → c");
    expect(item.subtitle).toContain("incident");
  });

  // The self frame (D18): the same four shapes, read by `details.method`
  // rather than by the scope the panel is in.
  const shift: ProportionShiftFinding = {
    ...base,
    type: "proportion_shift",
    field: "attr:user",
    value: "eve",
    count: 500,
    baseline_count: 0,
    baseline_rate: 0,
    window_rate: 0.31,
    rate_ratio: 15.5,
    direction: "up",
    g_statistic: 184.2,
    p_value: 1e-40,
    q_value: 1.2e-38,
    score: 184.2,
    first_seen: "2026-02-02T02:00:00Z",
    event_id: "e5",
  };

  it("reads a slice-mode proportion shift as slice vs the rest of the timeline", () => {
    const item = normalizeFinding(
      DETECTORS_BY_ID.shift,
      { ...shift, details: { method: "self-g-test", window_label: "slice 09/24" } },
      0,
    );
    expect(item.subtitle).toBe("share 31.00% in slice 09/24 vs 0.00% elsewhere (up, q=1.2e-38)");
    const temporal = normalizeFinding(DETECTORS_BY_ID.shift, { ...shift, details: { method: "g-test" } }, 0);
    expect(temporal.subtitle).toBe("share 0.00% → 31.00% (up, q=1.2e-38)");
  });

  it("reads a whole-scope cadence finding by its own numbers", () => {
    const cadence: IntervalPeriodicityFinding = {
      ...base,
      type: "interval_periodicity",
      field: "attr:svc",
      value: "beacon",
      direction: "missed",
      count: 4320,
      baseline_count: 4320,
      baseline_median_interval: 60,
      window_median_interval: 60,
      baseline_cv: 0.05,
      window_cv: null,
      statistic: 660,
      p_value: 1e-20,
      q_value: 3e-19,
      score: 18.5,
      first_seen: "2026-02-01T16:39:00Z",
      event_id: "e6",
      details: { method: "self-cadence", longest_gap_seconds: 660, median_interval: 60 },
    };
    const item = normalizeFinding(DETECTORS_BY_ID.interval, cadence, 0);
    expect(item.subtitle).toBe("silent for 660s vs median gap 60.0s (q=3.0e-19)");
    const beacon = normalizeFinding(
      DETECTORS_BY_ID.interval,
      {
        ...cadence,
        direction: "new_regularity",
        window_cv: 0.02,
        details: { method: "self-cadence", paused_intervals: 2 },
      },
      0,
    );
    expect(beacon.subtitle).toBe("regular cadence (beaconing), CV 0.02, 2 pauses excluded (q=3.0e-19)");
  });

  it("reads a rare ordering without a window", () => {
    const f: SequenceNoveltyFinding = {
      ...base,
      type: "sequence_novelty",
      field: "attr:act",
      values: ["a", "x", "y"],
      value: "a → x → y",
      count: 1,
      score: 7.6,
      first_seen: "2026-02-02T00:00:00Z",
      event_id: "e7",
      details: { method: "rare-ngram", scope_ngram_total: 2998, rarity_floor: 3 },
    };
    expect(normalizeFinding(DETECTORS_BY_ID.sequence, f, 0).subtitle).toBe(
      "rare ordering · ×1 across the timeline",
    );
  });

  it("names the slice for drift and never renders an undefined label", () => {
    const drift: DistributionDriftFinding = {
      ...base,
      type: "value_distribution_drift",
      field: "attr:bytes",
      window_label: "slice 09/24",
      test: "ks",
      statistic: 0.4,
      effect: 0.4,
      direction: "up",
      baseline_n: 24500,
      window_n: 500,
      p_value: 1e-30,
      q_value: 2e-29,
      score: 28.7,
      first_seen: "2026-02-02T02:00:00Z",
      event_id: "e8",
      details: { method: "self-drift" },
    };
    expect(normalizeFinding(DETECTORS_BY_ID.drift, drift, 0).subtitle).toBe(
      "KS up in slice 09/24 vs the rest (q=2.0e-29)",
    );
    const legacy = { ...drift, window_label: undefined as unknown as string, details: {} };
    expect(normalizeFinding(DETECTORS_BY_ID.drift, legacy, 0).subtitle).toBe(
      "KS up in the suspect window (q=2.0e-29)",
    );
  });
});

describe("interleaveByRank", () => {
  const mk = (detectorId: string, rank: number): FeedItem =>
    ({ detectorId, rank, title: `${detectorId}#${rank}` }) as unknown as FeedItem;

  it("emits every detector's rank-0 item before any rank-1 item", () => {
    const a = [mk("novelty", 0), mk("novelty", 1), mk("novelty", 2)];
    const b = [mk("frequency", 0)];
    const c = [mk("order", 0), mk("order", 1)];
    const out = interleaveByRank([a, b, c]);
    expect(out.map((i) => i.title)).toEqual([
      "novelty#0",
      "frequency#0",
      "order#0",
      "novelty#1",
      "order#1",
      "novelty#2",
    ]);
  });

  it("handles empty input", () => {
    expect(interleaveByRank([])).toEqual([]);
    expect(interleaveByRank([[], []])).toEqual([]);
  });
});
