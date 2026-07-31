import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Trash2 } from "lucide-react";
import { savedChartsApi } from "@/api/viz";
import { Button } from "@/components/ui/Button";
import { AddToStoryButton } from "@/components/stories/AddToStoryButton";
import { Input } from "@/components/ui/Input";
import {
  chartConfigToStored,
  parseStoredChartConfig,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";
import type { EventFilters } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  currentConfig: ChartConfig;
  /** The Explorer filters the chart is currently drawn under. Saved with the
   * chart, so a story block, an export and a re-load all reproduce the slice
   * the analyst was looking at rather than the whole timeline. Pass the raw
   * URL filters, not a set augmented with `collapseRoutine` — that one is not
   * URL-serialized and derives from live dispositions. */
  currentFilters: EventFilters;
  /** Load a saved chart by *id*, not by value. The page addresses it as
   * `?c_chart=<id>` and reads both halves — shape and filters — back out of
   * storage, which is the one place they travel together. Passing a parsed
   * config here instead would force the scope through the URL, where three
   * of its members (`ids`, `anomalyRunId`, `collapseRoutine`) have no
   * representation and would be silently dropped. */
  onLoad: (chartId: string) => void;
}

/**
 * Rail footer for saved charts: name-and-save the current ChartConfig plus the
 * filters it is drawn under, load a saved one back by id (with a graceful
 * message when it was saved by an incompatible config version), delete stale
 * ones.
 */
export function SavedChartsRail({
  caseId,
  timelineId,
  currentConfig,
  currentFilters,
  onLoad,
}: Props) {
  const [name, setName] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const qc = useQueryClient();
  const queryKey = ["viz-saved-charts", caseId, timelineId];

  const chartsQuery = useQuery({
    queryKey,
    queryFn: () => savedChartsApi.list(caseId, timelineId),
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      savedChartsApi.create(
        caseId,
        timelineId,
        name.trim(),
        chartConfigToStored(currentConfig, currentFilters),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      setName("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (chartId: string) => savedChartsApi.delete(caseId, timelineId, chartId),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  // Version compatibility is checked here rather than after navigating: the
  // rail knows the stored bytes already, and an incompatible chart should say
  // so in place instead of loading a URL that renders nothing.
  const handleLoad = (chartId: string, stored: Record<string, unknown>) => {
    if (parseStoredChartConfig(stored) == null) {
      setLoadError("This chart was saved with an incompatible version and cannot be loaded.");
      return;
    }
    setLoadError(null);
    onLoad(chartId);
  };

  const charts = chartsQuery.data?.charts ?? [];

  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
        Saved charts
      </label>
      <div className="flex items-center gap-1">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim() && !saveMutation.isPending) {
              saveMutation.mutate();
            }
          }}
          placeholder="Save current chart as…"
          className="h-7 flex-1 text-xs"
        />
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-1.5"
          disabled={!name.trim() || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
          aria-label="Save chart"
        >
          <Save size={13} />
        </Button>
      </div>
      {saveMutation.error && (
        <p className="text-xs text-[var(--color-danger)]">
          {(saveMutation.error as Error).message}
        </p>
      )}
      {loadError && <p className="text-xs text-[var(--color-danger)]">{loadError}</p>}
      {charts.length > 0 && (
        <ul className="space-y-0.5">
          {charts.map((c) => (
            <li key={c.id} className="group flex items-center gap-1">
              <button
                onClick={() => handleLoad(c.id, c.config)}
                className="flex-1 truncate rounded px-1.5 py-1 text-left text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)]"
                title={`Load "${c.name}"`}
              >
                {c.name}
              </button>
              <AddToStoryButton
                caseId={caseId}
                iconOnly
                className="h-6 w-6 px-0 opacity-0 group-hover:opacity-100"
                label={`Add "${c.name}" to a story`}
                content={{
                  kind: "chart_ref",
                  content: { chart_id: c.id, timeline_id: timelineId },
                }}
              />
              <button
                onClick={() => deleteMutation.mutate(c.id)}
                className="rounded p-1 text-[var(--color-fg-muted)] opacity-0 hover:text-[var(--color-danger)] group-hover:opacity-100"
                aria-label={`Delete saved chart ${c.name}`}
              >
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
