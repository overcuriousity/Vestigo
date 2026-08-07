import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { enrichersApi, type TimelineEnricherInfo } from "@/api/enrichers";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Switch } from "@/components/ui/Switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { useJobsStore } from "@/stores/jobs";
import { toast } from "@/stores/toasts";
import type { Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timeline: Timeline;
}

/**
 * Per-timeline enricher configuration: enable/disable, automatic vs. manual
 * trigger mode, and a manual "Run now". Only enrichers that currently pass
 * their availability check are listed at all — an unavailable enricher (e.g.
 * GeoIP with no uploaded database) simply does not appear.
 */
export function EnrichersDialog({ caseId, timeline }: Props) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);

  const queryKey = ["timeline-enrichers", caseId, timeline.id] as const;
  const configMutationKey = ["enricher-config", caseId, timeline.id] as const;

  const { data: enrichers, isLoading } = useQuery({
    queryKey,
    queryFn: () => enrichersApi.listForTimeline(caseId, timeline.id),
    enabled: open,
  });

  // Optimistic update: patch the cached list synchronously in onMutate so a
  // rapid toggle-then-mode-change reads the fresh value instead of the stale
  // pre-mutation snapshot (lost-update race).
  const configMutation = useMutation({
    mutationKey: configMutationKey,
    mutationFn: (vars: { key: string; mode: "automatic" | "manual"; enabled: boolean }) =>
      enrichersApi.setConfig(caseId, timeline.id, vars.key, {
        mode: vars.mode,
        enabled: vars.enabled,
      }),
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey });
      const previous = qc.getQueryData<TimelineEnricherInfo[]>(queryKey);
      qc.setQueryData<TimelineEnricherInfo[]>(queryKey, (old) =>
        old?.map((e) =>
          e.key === vars.key ? { ...e, mode: vars.mode, enabled: vars.enabled } : e,
        ),
      );
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous) qc.setQueryData(queryKey, ctx.previous);
    },
    onSettled: () => {
      // Only refetch after the last in-flight config mutation settles, so an
      // earlier response doesn't clobber a later optimistic patch.
      if (qc.isMutating({ mutationKey: configMutationKey }) === 1) {
        qc.invalidateQueries({ queryKey });
      }
    },
    meta: { errorTitle: "Enricher config change failed" },
  });

  const runMutation = useMutation({
    mutationFn: (vars: { key: string; force?: boolean }) =>
      enrichersApi.run(caseId, timeline.id, vars.key, vars.force),
    onSuccess: (res, vars) => {
      // Skipped run: every ready source already enriched at the current config
      // (same enricher + data version), so no job started — say so instead of
      // letting the click look like it did nothing.
      if (res.job_id === null) {
        toast.info(
          `${vars.key}: already enriched`,
          "Every ready source is up to date — no job started. If events are missing derived fields anyway, use Force re-run.",
        );
        return;
      }
      addJob(res.job_id, `${vars.key} enrichment`, [
        ["events", caseId, timeline.id],
        ["fields", caseId, timeline.id],
      ]);
    },
  });

  // Applies an interrupted run's already-staged results. Invalidates the list
  // on settle so the banner clears (success) or stays (failure).
  const resumeMutation = useMutation({
    mutationFn: (vars: { key: string; jobId: string }) =>
      enrichersApi.resume(caseId, timeline.id, vars.key, vars.jobId),
    onSuccess: (res, vars) => {
      addJob(res.job_id, `${vars.key} enrichment (resume)`, [
        ["events", caseId, timeline.id],
        ["fields", caseId, timeline.id],
      ]);
    },
    onSettled: () => qc.invalidateQueries({ queryKey }),
    meta: { errorTitle: "Could not resume the enrichment run" },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title="Enrichers">
          <Sparkles size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Enrichers"
        description="Derive additional fields from this timeline's events. Enrichers not listed here are currently unavailable — an admin may need to enable a required asset."
        className="max-w-xl"
      >
        <div className="space-y-3">
          {isLoading && <p className="text-xs text-[var(--color-fg-muted)]">Loading…</p>}
          {enrichers?.length === 0 && (
            <p className="text-xs text-[var(--color-fg-muted)]">
              No enrichers are currently available for this timeline.
            </p>
          )}
          {enrichers?.map((e) => (
            <EnricherRow
              key={e.key}
              enricher={e}
              onToggle={(enabled) =>
                configMutation.mutate({ key: e.key, mode: e.mode, enabled })
              }
              onModeChange={(mode) =>
                configMutation.mutate({ key: e.key, mode, enabled: e.enabled })
              }
              onRun={(force) => runMutation.mutate({ key: e.key, force })}
              onResume={() =>
                e.unfinished_run &&
                resumeMutation.mutate({ key: e.key, jobId: e.unfinished_run.job_id })
              }
              isRunning={runMutation.isPending && runMutation.variables?.key === e.key}
              isResuming={resumeMutation.isPending && resumeMutation.variables?.key === e.key}
            />
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** "3 minutes", "2 days" — coarse on purpose; the exact timestamp is in the title. */
function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} seconds`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86400)} days`;
}

function EnricherRow({
  enricher,
  onToggle,
  onModeChange,
  onRun,
  onResume,
  isRunning,
  isResuming,
}: {
  enricher: TimelineEnricherInfo;
  onToggle: (enabled: boolean) => void;
  onModeChange: (mode: "automatic" | "manual") => void;
  onRun: (force: boolean) => void;
  onResume: () => void;
  isRunning: boolean;
  isResuming: boolean;
}) {
  const unfinished = enricher.unfinished_run;
  const partial =
    unfinished !== null && unfinished.completed_sources < unfinished.staged_sources;
  return (
    <div className="rounded-lg border border-[var(--color-border)] p-3 space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-[var(--color-fg-primary)]">
            {enricher.display_name}
          </p>
          <p className="text-xs text-[var(--color-fg-muted)]">{enricher.description}</p>
        </div>
        <Switch checked={enricher.enabled} onCheckedChange={onToggle} />
      </div>
      {unfinished && (
        <div className="rounded-md border border-[var(--color-warning)] bg-[var(--color-warning-dim)] p-2 space-y-1.5">
          <p className="text-xs font-medium text-[var(--color-fg-primary)]">
            An earlier run was interrupted before its results were saved
          </p>
          <p className="text-xs text-[var(--color-fg-muted)]">
            {unfinished.staged_rows.toLocaleString()}{" "}
            {unfinished.staged_rows === 1 ? "event" : "events"} across{" "}
            {unfinished.staged_sources}{" "}
            {unfinished.staged_sources === 1 ? "source" : "sources"} were enriched{" "}
            {formatAge(unfinished.age_seconds)} ago but never written to the timeline.
            Resuming applies them — no re-scan.
            {partial &&
              " One source was only partly processed; its values still apply, and it stays eligible for a later run."}
          </p>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              disabled={isResuming}
              onClick={onResume}
              title={`Interrupted run ${unfinished.job_id}, started ${unfinished.started_at}`}
            >
              {isResuming ? "Resuming…" : "Resume"}
            </Button>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between gap-3 text-xs text-[var(--color-fg-muted)]">
        <span>
          {enricher.eligibility_error
            ? "Could not check this timeline for matching field values"
            : enricher.eligible
              ? "Matching field values found in this timeline's sources"
              : "No matching field values found in this timeline's sources"}
        </span>
        <div className="flex items-center gap-2 shrink-0">
          <Select
            value={enricher.mode}
            onValueChange={(v) => onModeChange(v as "automatic" | "manual")}
            disabled={!enricher.enabled}
          >
            <SelectTrigger className="h-7 w-28 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="automatic">Automatic</SelectItem>
              <SelectItem value="manual">Manual</SelectItem>
            </SelectContent>
          </Select>
          {/* Force re-run is a standing affordance, not a state that only
              appears after a skipped run in this dialog session — it is the
              documented recovery path when provenance disagrees with the
              events, and it has to survive a page refresh to be usable. */}
          <Button
            variant="ghost"
            size="sm"
            disabled={!enricher.enabled || isRunning || unfinished !== null}
            title="Re-enrich every ready source, ignoring the already-enriched record — use when derived fields are missing despite 'up to date'"
            onClick={() => onRun(true)}
          >
            Force re-run
          </Button>
          {/* Deliberately not disabled on `eligible`: that is a heuristic
              sample scan (and is false outright when the check failed), so it
              must never be the thing that locks an analyst out of running. */}
          <Button
            variant="ghost"
            size="sm"
            disabled={!enricher.enabled || isRunning || unfinished !== null}
            title={
              unfinished !== null
                ? "Resume the interrupted run first — a new run would strand its results"
                : enricher.eligible
                  ? undefined
                  : "No matching values were found, but you can still run it"
            }
            onClick={() => onRun(false)}
          >
            {isRunning ? "Running…" : "Run now"}
          </Button>
        </div>
      </div>
    </div>
  );
}
