/**
 * ChartProposalCard — renders an agent `propose_chart` tool call as a live
 * chart card: title, explanation, the chart itself (fetched fresh through
 * the same `vizApi` the Visualize page uses, not the tool_result echo — see
 * module doc below), "Open in Visualize" and "Save".
 *
 * Sandbox + apply model, same as FindingCard: the agent never writes
 * anything. "Save" is the analyst's own click against the existing
 * `savedChartsApi.create` — the only write in this flow, credited to the
 * analyst, mirroring how `propose_finding`'s "Apply to Explorer" is the
 * analyst's own action.
 *
 * Live fetch (not the tool_result summary echo) keeps the chart consistent
 * with the analyst's current data/dispositions — the summary the tool returned
 * is a validation receipt for the model, not a display value, and is
 * deliberately not shown here.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BarChart3, ExternalLink, Save } from "lucide-react";
import { savedChartsApi } from "@/api/viz";
import { chartConfigToParams, chartConfigToStored } from "@/components/viz/lib/chartConfig";
import { ChartCanvas } from "@/components/viz/ChartCanvas";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { Markdown } from "./Markdown";
import { specToChartConfig, specToEventFilters, type AgentChartSpec } from "@/api/agent";
import { filtersToParams } from "@/lib/queryParams";
interface Props {
  caseId: string;
  timelineId: string;
  title: string;
  description: string;
  spec: AgentChartSpec;
}

export function ChartProposalCard({ caseId, timelineId, title, description, spec }: Props) {
  const config = useMemo(() => specToChartConfig(spec), [spec]);
  const filters = useMemo(() => specToEventFilters(spec.filters), [spec]);

  const qc = useQueryClient();
  const [name, setName] = useState("");
  const saveMutation = useMutation({
    mutationFn: () =>
      savedChartsApi.create(
        caseId,
        timelineId,
        name.trim(),
        // The spec's own filters travel with the chart: the card renders the
        // filtered chart, so saving the shape alone would hand the analyst a
        // saved chart that draws something else.
        chartConfigToStored(config, filters),
      ),
    onSuccess: () => {
      setName("");
      // Same key SavedChartsRail reads, so an open Visualize page picks the
      // new chart up instead of showing a stale rail.
      qc.invalidateQueries({ queryKey: ["viz-saved-charts", caseId, timelineId] });
    },
  });

  const openParams = chartConfigToParams(config, filtersToParams(filters));
  const openHref = `/cases/${caseId}/timelines/${timelineId}/visualize?${openParams.toString()}`;

  return (
    <div className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-dim)] p-2.5 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-primary)]">
        <BarChart3 size={13} className="shrink-0 text-[var(--color-accent)]" />
        <span className="min-w-0 break-words">{title}</span>
      </div>
      {description && (
        <div className="mt-1 text-[var(--color-fg-secondary)]">
          <Markdown content={description} />
        </div>
      )}

      <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2">
        <ChartCanvas
          caseId={caseId}
          timelineId={timelineId}
          config={config}
          filters={filters}
          incompleteMessage="This chart proposal is missing a field, so there is nothing to plot."
          testId="agent-chart-canvas"
        />
      </div>

      <div className="mt-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && name.trim() && !saveMutation.isPending) {
                saveMutation.mutate();
              }
            }}
            placeholder="Save as…"
            className="h-6 w-28 text-[11px]"
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5"
            disabled={!name.trim() || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
            aria-label="Save chart"
          >
            {saveMutation.isPending ? <Spinner size={11} /> : <Save size={12} />}
          </Button>
          {saveMutation.isSuccess && (
            <span className="text-[10px] text-[var(--color-success)]">Saved</span>
          )}
        </div>
        <Button variant="accent" size="sm" asChild>
          <Link to={openHref}>
            Open in Visualize
            <ExternalLink size={12} />
          </Link>
        </Button>
      </div>
    </div>
  );
}
