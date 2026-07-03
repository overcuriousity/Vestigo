import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Trash2 } from "lucide-react";
import { savedChartsApi } from "@/api/viz";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import {
  chartConfigToStored,
  parseStoredChartConfig,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";

interface Props {
  caseId: string;
  timelineId: string;
  currentConfig: ChartConfig;
  onLoad: (config: ChartConfig) => void;
}

/**
 * Rail footer for saved charts: name-and-save the current ChartConfig, load
 * a saved one (with a graceful message when it was saved by an incompatible
 * config version), delete stale ones.
 */
export function SavedChartsRail({ caseId, timelineId, currentConfig, onLoad }: Props) {
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
      savedChartsApi.create(caseId, timelineId, name.trim(), chartConfigToStored(currentConfig)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      setName("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (chartId: string) => savedChartsApi.delete(caseId, timelineId, chartId),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  const handleLoad = (stored: Record<string, unknown>) => {
    const config = parseStoredChartConfig(stored);
    if (config == null) {
      setLoadError("This chart was saved with an incompatible version and cannot be loaded.");
      return;
    }
    setLoadError(null);
    onLoad(config);
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
                onClick={() => handleLoad(c.config)}
                className="flex-1 truncate rounded px-1.5 py-1 text-left text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)]"
                title={`Load "${c.name}"`}
              >
                {c.name}
              </button>
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
