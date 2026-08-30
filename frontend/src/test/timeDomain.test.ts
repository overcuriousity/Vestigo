import { describe, expect, it } from "vitest";
import {
  cumulativeChartDomain,
  lanesChartDomain,
  timeChartDomain,
  timeseriesChartDomain,
} from "@/components/viz/lib/timeDomain";

const iso = (d: [Date, Date] | null) => d?.map((x) => x.toISOString());

describe("time-axis domains", () => {
  it("kind=time spans the first and last bucket start", () => {
    expect(
      iso(
        timeChartDomain({
          buckets: [
            { start: "2026-07-20T00:00:00+00:00" },
            { start: "2026-07-20T01:00:00+00:00" },
          ],
        }),
      ),
    ).toEqual(["2026-07-20T00:00:00.000Z", "2026-07-20T01:00:00.000Z"]);
    expect(timeChartDomain({ buckets: [] })).toBeNull();
  });

  it("a single bucket is a point, not an inverted domain", () => {
    expect(iso(timeChartDomain({ buckets: [{ start: "2026-07-20T00:00:00+00:00" }] }))).toEqual([
      "2026-07-20T00:00:00.000Z",
      "2026-07-20T00:00:00.000Z",
    ]);
  });

  it("kind=timeseries reads series 0", () => {
    expect(
      iso(
        timeseriesChartDomain({
          series: [
            { buckets: [{ start: "2026-07-20T00:00:00+00:00" }, { start: "2026-07-20T02:00:00+00:00" }] },
          ],
        }),
      ),
    ).toEqual(["2026-07-20T00:00:00.000Z", "2026-07-20T02:00:00.000Z"]);
    expect(timeseriesChartDomain({ series: [] })).toBeNull();
  });

  it("kind=cumulative ends one bucket after the last start, or at max", () => {
    const base = {
      buckets: [{ start: "2026-07-20T00:00:00+00:00" }, { start: "2026-07-20T01:00:00+00:00" }],
      interval_seconds: 3600,
      min: "2026-07-20T00:00:00+00:00",
    };
    // The step holds through the last bucket.
    expect(iso(cumulativeChartDomain({ ...base, max: "2026-07-20T01:30:00+00:00" }))).toEqual([
      "2026-07-20T00:00:00.000Z",
      "2026-07-20T02:00:00.000Z",
    ]);
    // …unless the last event is later still.
    expect(iso(cumulativeChartDomain({ ...base, max: "2026-07-20T05:00:00+00:00" }))).toEqual([
      "2026-07-20T00:00:00.000Z",
      "2026-07-20T05:00:00.000Z",
    ]);
    expect(cumulativeChartDomain({ ...base, buckets: [], max: null, min: null })).toBeNull();
  });

  it("kind=lanes is the paired slice, and null when it has none", () => {
    expect(
      iso(
        lanesChartDomain({
          slice_start: "2026-07-20T00:00:00+00:00",
          slice_end: "2026-07-21T00:00:00+00:00",
        }),
      ),
    ).toEqual(["2026-07-20T00:00:00.000Z", "2026-07-21T00:00:00.000Z"]);
    expect(lanesChartDomain({ slice_start: null, slice_end: null })).toBeNull();
  });
});
