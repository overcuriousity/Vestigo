/**
 * MiniSparkline — inline proportional bar chart for a series of counts.
 *
 * No chart dependency — hand-rolled div bars (airgap-safe), same idiom as
 * TimelineHistogram.tsx. Width/height are controlled via className.
 */
interface Props {
  /** Raw count values; index = time order left → right */
  buckets: number[];
  /** CSS class applied to the outer container */
  className?: string;
  /** Accent color override (CSS colour value). Defaults to var(--color-accent). */
  color?: string;
  /** If supplied, bar at this index is highlighted with the anomaly color. */
  anomalyIndex?: number;
}

export function MiniSparkline({
  buckets,
  className = "h-4 w-24",
  color,
  anomalyIndex,
}: Props) {
  const maxVal = Math.max(1, ...buckets);

  return (
    <div
      className={`flex items-end gap-px overflow-hidden ${className}`}
      aria-hidden="true"
    >
      {buckets.map((v, i) => {
        const heightPct = Math.max(8, Math.round((v / maxVal) * 100));
        const isAnomaly = i === anomalyIndex;
        const barColor = isAnomaly
          ? "var(--color-error, #f87171)"
          : (color ?? "var(--color-accent)");
        return (
          <div
            key={i}
            className="flex-1 rounded-t-[1px]"
            style={{
              height: `${heightPct}%`,
              backgroundColor: barColor,
              opacity: isAnomaly ? 1 : 0.4,
              minWidth: 2,
            }}
          />
        );
      })}
    </div>
  );
}
