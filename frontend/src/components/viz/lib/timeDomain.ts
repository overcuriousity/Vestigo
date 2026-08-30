/**
 * The time domain each time-axis figure draws — one pure function per
 * figure, so the x scale a chart builds and the axis the caption reasons
 * about are the same interval by construction.
 *
 * `lib/marks.ts` needs this: a mark outside the drawn axis is not rendered,
 * and a caption that counted it anyway would name rules the reader cannot
 * find. The charts import these instead of computing the domain inline, so
 * the two can never drift apart.
 */

/** Bucket starts as dates, in the order the server returned them. */
function starts(buckets: readonly { start: string }[]): Date[] {
  return buckets.map((b) => new Date(b.start));
}

/** kind=time (CompareHistogram): first bucket start → last bucket start. */
export function timeChartDomain(data: {
  buckets: readonly { start: string }[];
}): [Date, Date] | null {
  const dates = starts(data.buckets);
  if (dates.length === 0) return null;
  return [dates[0], dates.length > 1 ? dates[dates.length - 1] : dates[0]];
}

/** kind=timeseries (LineChart): first → last bucket start of series 0. */
export function timeseriesChartDomain(data: {
  series: readonly { buckets: readonly { start: string }[] }[];
}): [Date, Date] | null {
  if (data.series.length === 0) return null;
  const dates = starts(data.series[0].buckets);
  if (dates.length === 0) return null;
  return [dates[0], dates[dates.length - 1]];
}

/**
 * kind=cumulative (CumulativeStep): the step holds through the last bucket,
 * so the axis ends one bucket after the last start (or at `max` if later).
 */
export function cumulativeChartDomain(data: {
  buckets: readonly { start: string }[];
  interval_seconds: number;
  min: string | null;
  max: string | null;
}): [Date, Date] | null {
  const dates = starts(data.buckets);
  if (dates.length === 0 || data.min == null || data.max == null) return null;
  const lastEnd = new Date(
    Math.max(
      dates[dates.length - 1].getTime() + data.interval_seconds * 1000,
      Date.parse(data.max),
    ),
  );
  return [dates[0], lastEnd];
}

/** kind=lanes (IntervalLanes): the slice the server paired over — null when
 * it found no dated events to bound one with. */
export function lanesChartDomain(data: {
  slice_start: string | null;
  slice_end: string | null;
}): [Date, Date] | null {
  if (!data.slice_start || !data.slice_end) return null;
  return [new Date(data.slice_start), new Date(data.slice_end)];
}
