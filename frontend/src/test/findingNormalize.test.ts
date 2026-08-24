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
  FrequencyFinding,
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
