import { format as formatNum } from "d3-format";
import { Spinner } from "@/components/ui/Spinner";
import { fieldTokenLabel, fieldValueLabel } from "@/components/viz/lib/fieldDisplay";

const fmtInt = formatNum(",d");

export interface FacetPanel {
  value: string;
  count?: number;
  isLoading: boolean;
  chart: React.ReactNode;
}

interface FacetGridProps {
  field: string;
  panels: FacetPanel[];
  /** Values outside the drawn top-N, and how many events they hold. */
  omittedValues?: number;
  omittedCount?: number;
}

/**
 * Small-multiple grid (Tufte 1990): the same mark repeated once per facet
 * value, so differences between subsets are read as position in one glance
 * instead of remembered across chart switches.
 *
 * Panels are the facet field's top values by event count. The rest are
 * **omitted, not merged**: an "Other" panel would be a distribution of
 * unrelated subsets pretending to be one. The omission is stated under the
 * grid and repeated in the chart caption/export.
 *
 * Comparability is the whole point of the layout, so panels are equal-sized
 * and rendered by the same component with the same options; each panel's
 * own chart still owns its axes.
 */
export function FacetGrid({ field, panels, omittedValues, omittedCount }: FacetGridProps) {
  if (panels.length === 0) {
    return (
      <div className="rounded border border-[var(--color-border)] p-4 text-sm text-[var(--color-fg-muted)]">
        No values of {fieldTokenLabel(field)} match the current filters.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {panels.map((panel) => (
          <div
            key={panel.value}
            className="rounded border border-[var(--color-border)] p-2"
          >
            <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate font-medium text-[var(--color-fg-primary)]">
                {fieldTokenLabel(field)} = {fieldValueLabel(field, panel.value)}
              </span>
              {panel.count != null && (
                <span className="shrink-0 text-[var(--color-fg-muted)]">
                  {fmtInt(panel.count)} events
                </span>
              )}
            </div>
            {panel.isLoading ? (
              <div className="flex h-32 items-center justify-center">
                <Spinner />
              </div>
            ) : (
              panel.chart
            )}
          </div>
        ))}
      </div>
      {!!omittedValues && (
        <p className="text-xs text-[var(--color-fg-muted)]">
          {fmtInt(omittedValues)} further value{omittedValues === 1 ? "" : "s"} of{" "}
          {fieldTokenLabel(field)}
          {omittedCount != null && omittedCount > 0
            ? ` (${fmtInt(omittedCount)} events)`
            : ""}{" "}
          are not shown — they are omitted, not merged into an "Other" panel.
        </p>
      )}
    </div>
  );
}
