import { format as formatNum } from "d3-format";
import { ExplainerPopover } from "@/components/viz/primitives/ExplainerPopover";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.4~f");
const fmtInt = formatNum(",d");

/**
 * Compact teaching strip under numeric charts — the summary statistics the
 * server computed, each with its explainer popover. The numbers here are the
 * exact server-computed values echoed in the caption/export; nothing is
 * recomputed client-side.
 */
export function NumericStatStrip({ stats }: { stats: FieldNumericResponse }) {
  if (stats.count === 0) return null;
  const median = stats.quantiles["0.5"];
  const g1 = stats.skewness;
  const skewReading =
    g1 == null
      ? null
      : Math.abs(g1) < 0.5
        ? "≈ symmetric"
        : g1 > 0
          ? "right-skewed"
          : "left-skewed";

  const item = "flex items-center gap-1 whitespace-nowrap";
  const label = "text-[var(--color-fg-muted)]";
  const value = "font-medium text-[var(--color-fg-primary)]";

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--color-fg-secondary)]">
      <span className={item}>
        <span className={label}>n</span> <span className={value}>{fmtInt(stats.count)}</span>
      </span>
      {stats.mean != null && (
        <span className={item}>
          <span className={label}>mean</span> <span className={value}>{fmtValue(stats.mean)}</span>
          <ExplainerPopover id="mean" />
        </span>
      )}
      {median != null && (
        <span className={item}>
          <span className={label}>median</span> <span className={value}>{fmtValue(median)}</span>
          <ExplainerPopover id="median" />
        </span>
      )}
      {stats.stddev != null && (
        <span className={item}>
          <span className={label}>σ</span> <span className={value}>{fmtValue(stats.stddev)}</span>
        </span>
      )}
      {g1 != null && (
        <span className={item}>
          <span className={label}>skewness</span>{" "}
          <span className={value}>
            {fmtValue(g1)} ({skewReading})
          </span>
          <ExplainerPopover id="skewness" />
        </span>
      )}
    </div>
  );
}
