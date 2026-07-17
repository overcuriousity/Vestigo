/**
 * SigmaPanel — the Sigma tab of the InvestigatePanel.
 *
 * Deterministic signature matching, deliberately separate from the
 * statistical detectors: analysts pick rules (admin-managed global set +
 * case uploads), run them as a background job over the timeline, and every
 * hit lands as a system annotation whose "sigma: <title>" label is
 * filterable in the unified tag panel. Run history shows per-rule outcomes
 * with the compiled SQL for forensic review.
 */
import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Filter,
  Play,
  Trash2,
  Upload,
} from "lucide-react";
import { sigmaApi } from "@/api/sigma";
import type { SigmaRuleInfo, SigmaRun, SigmaRunResult } from "@/api/sigma";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { useJobsStore } from "@/stores/jobs";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
  /** Applies a tags-include filter in the Explorer grid ("view hits"). */
  onTagFilter?: (tag: string) => void;
}

const LEVEL_VARIANT: Record<string, "muted" | "default" | "accent" | "anomaly" | "danger"> = {
  informational: "muted",
  low: "default",
  medium: "accent",
  high: "anomaly",
  critical: "danger",
};

function ruleId(r: SigmaRuleInfo): string {
  return `${r.origin}:${r.ref}`;
}

function logsourceText(r: SigmaRuleInfo): string {
  const ls = r.logsource ?? {};
  return [ls.product, ls.category, ls.service].filter(Boolean).join(" / ");
}

export function SigmaPanel({ caseId, timelineId, onTagFilter }: Props) {
  const qc = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Deselected rules (default: everything enabled runs) — keyed origin:ref.
  const [deselected, setDeselected] = useState<Set<string>>(new Set());
  const [expandedRun, setExpandedRun] = useState<string | null>(null);

  const { data: ruleData } = useQuery({
    queryKey: ["sigma-rules", caseId],
    queryFn: () => sigmaApi.listRules(caseId),
  });
  const { data: runs = [] } = useQuery({
    queryKey: ["sigma-runs", caseId],
    queryFn: () => sigmaApi.listRuns(caseId),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((r) => r.status === "queued" || r.status === "running")
        ? 2000
        : false,
  });

  const allRules = useMemo(() => {
    const globals = ruleData?.global_rules ?? [];
    const cases = ruleData?.case_rules ?? [];
    return [...cases, ...globals];
  }, [ruleData]);
  const runnable = allRules.filter((r) => r.enabled && !r.error);
  const selectedRules = runnable.filter((r) => !deselected.has(ruleId(r)));

  const uploadMutation = useMutation({
    mutationFn: (yaml: string) => sigmaApi.uploadRule(caseId, yaml),
    onSuccess: () => {
      setUploadError(null);
      qc.invalidateQueries({ queryKey: ["sigma-rules", caseId] });
    },
    onError: (err: Error) => setUploadError(err.message),
  });

  const toggleEnabled = useMutation({
    mutationFn: (r: SigmaRuleInfo) => sigmaApi.setRuleEnabled(caseId, r.ref, !r.enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sigma-rules", caseId] }),
  });

  const deleteRule = useMutation({
    mutationFn: (r: SigmaRuleInfo) => sigmaApi.deleteRule(caseId, r.ref),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sigma-rules", caseId] }),
  });

  const runMutation = useMutation({
    mutationFn: () =>
      sigmaApi.run(
        caseId,
        timelineId,
        // All runnable rules selected → omit the list (server runs everything enabled).
        selectedRules.length === runnable.length
          ? null
          : selectedRules.map((r) => ({ origin: r.origin, ref: r.ref })),
      ),
    onSuccess: (res) => {
      setExpandedRun(res.run_id);
      addJob(res.job_id, "Sigma scan", [
        ["sigma-runs", caseId],
        ["events", caseId, timelineId],
        ["tags-merged", caseId, timelineId],
      ]);
      qc.invalidateQueries({ queryKey: ["sigma-runs", caseId] });
    },
  });

  const handleFiles = async (files: FileList | null) => {
    if (!files) return;
    for (const file of Array.from(files)) {
      uploadMutation.mutate(await file.text());
    }
  };

  return (
    <div className="space-y-4">
      <GuidancePanel id="investigate-sigma" title="How Sigma scanning works">
        <p>
          Sigma rules are community-standard YAML signatures for suspicious log patterns.
          Running them evaluates each rule against every event in this timeline; matches are
          tagged <span className="font-mono">sigma: &lt;rule title&gt;</span> as system
          annotations, filterable from the Tags panel. Signature matching is deterministic —
          it complements, not replaces, the statistical anomaly detectors.
        </p>
      </GuidancePanel>

      {/* Rules */}
      <section>
        <div className="mb-2 flex items-center gap-2">
          <h4 className="flex-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Rules
            <span className="ml-2 font-mono text-[10px] font-normal normal-case text-[var(--color-fg-muted)]">
              {selectedRules.length}/{runnable.length} selected
            </span>
          </h4>
          <input
            ref={fileInputRef}
            type="file"
            accept=".yml,.yaml"
            multiple
            className="hidden"
            onChange={(e) => {
              void handleFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <Button variant="outline" size="sm" onClick={() => fileInputRef.current?.click()}>
            <Upload size={12} className="mr-1" />
            Upload rule
          </Button>
          <Button
            size="sm"
            disabled={selectedRules.length === 0 || runMutation.isPending}
            onClick={() => runMutation.mutate()}
          >
            <Play size={12} className="mr-1" />
            Run {selectedRules.length}
          </Button>
        </div>
        {uploadError && (
          <p className="mb-2 text-xs text-[var(--color-danger)]">{uploadError}</p>
        )}
        {runMutation.isError && (
          <p className="mb-2 text-xs text-[var(--color-danger)]">
            {(runMutation.error as Error).message}
          </p>
        )}
        {!ruleData?.rules_path_configured && (ruleData?.case_rules ?? []).length === 0 && (
          <p className="mb-2 text-xs text-[var(--color-fg-muted)]">
            No global ruleset directory is configured (VESTIGO_SIGMA_RULES_PATH). Upload
            individual rules here, or ask an admin to provision an offline ruleset drop
            (e.g. a SigmaHQ clone).
          </p>
        )}
        <ul className="max-h-64 space-y-1 overflow-y-auto">
          {allRules.map((r) => (
            <li
              key={ruleId(r)}
              className={cn(
                "flex items-center gap-2 rounded border border-[var(--color-border)] px-2 py-1.5 text-xs",
                (!r.enabled || r.error) && "opacity-50",
              )}
            >
              <input
                type="checkbox"
                checked={r.enabled && !r.error && !deselected.has(ruleId(r))}
                disabled={!r.enabled || !!r.error}
                onChange={() =>
                  setDeselected((prev) => {
                    const next = new Set(prev);
                    const id = ruleId(r);
                    if (next.has(id)) next.delete(id);
                    else next.add(id);
                    return next;
                  })
                }
              />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-[var(--color-fg-primary)]" title={r.title}>
                  {r.title}
                </div>
                <div className="truncate font-mono text-[10px] text-[var(--color-fg-muted)]">
                  {r.origin === "global" ? r.ref : "uploaded"}
                  {logsourceText(r) && ` · ${logsourceText(r)}`}
                </div>
              </div>
              {r.error && (
                <span title={r.error}>
                  <AlertTriangle size={12} className="text-[var(--color-danger)]" />
                </span>
              )}
              {r.level && (
                <Badge variant={LEVEL_VARIANT[r.level] ?? "default"}>{r.level}</Badge>
              )}
              {r.origin === "case" && (
                <>
                  <button
                    className="text-[10px] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
                    onClick={() => toggleEnabled.mutate(r)}
                  >
                    {r.enabled ? "disable" : "enable"}
                  </button>
                  <button
                    className="text-[var(--color-fg-muted)] hover:text-[var(--color-danger)]"
                    onClick={() => deleteRule.mutate(r)}
                    title="Delete uploaded rule"
                  >
                    <Trash2 size={12} />
                  </button>
                </>
              )}
            </li>
          ))}
          {allRules.length === 0 && (
            <li className="py-2 text-xs text-[var(--color-fg-muted)]">No rules available yet.</li>
          )}
        </ul>
      </section>

      {/* Run history */}
      <section className="border-t border-[var(--color-border)] pt-3">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)]">
          Runs
        </h4>
        <ul className="space-y-1">
          {runs.map((run) => (
            <SigmaRunRow
              key={run.id}
              run={run}
              expanded={expandedRun === run.id}
              onToggle={() => setExpandedRun(expandedRun === run.id ? null : run.id)}
              onTagFilter={onTagFilter}
            />
          ))}
          {runs.length === 0 && (
            <li className="py-2 text-xs text-[var(--color-fg-muted)]">No runs yet.</li>
          )}
        </ul>
      </section>
    </div>
  );
}

const STATUS_VARIANT: Record<string, "success" | "muted" | "default" | "danger"> = {
  matched: "success",
  empty: "muted",
  not_applicable: "default",
  error: "danger",
};

function SigmaRunRow({
  run,
  expanded,
  onToggle,
  onTagFilter,
}: {
  run: SigmaRun;
  expanded: boolean;
  onToggle: () => void;
  onTagFilter?: (tag: string) => void;
}) {
  const results = run.results ?? [];
  const totalHits = results.reduce((n, r) => n + r.match_count, 0);
  return (
    <li className="rounded border border-[var(--color-border)]">
      <button
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs"
        onClick={onToggle}
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="flex-1 truncate text-[var(--color-fg-primary)]">
          {run.created_at ? new Date(run.created_at).toLocaleString() : run.id}
        </span>
        <Badge
          variant={
            run.status === "completed" ? "success" : run.status === "failed" ? "danger" : "accent"
          }
        >
          {run.status}
        </Badge>
        <span className="font-mono text-[10px] text-[var(--color-fg-muted)]">
          {results.length} rules · {totalHits} hits
        </span>
      </button>
      {expanded && (
        <div className="border-t border-[var(--color-border)] px-2 py-1.5">
          {run.error && <p className="mb-1 text-xs text-[var(--color-danger)]">{run.error}</p>}
          <ul className="space-y-1">
            {results.map((res) => (
              <SigmaResultRow key={res.rule_key} res={res} onTagFilter={onTagFilter} />
            ))}
            {results.length === 0 && (
              <li className="text-xs text-[var(--color-fg-muted)]">No per-rule results yet.</li>
            )}
          </ul>
        </div>
      )}
    </li>
  );
}

function SigmaResultRow({
  res,
  onTagFilter,
}: {
  res: SigmaRunResult;
  onTagFilter?: (tag: string) => void;
}) {
  const [showSql, setShowSql] = useState(false);
  return (
    <li className="text-xs">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate" title={res.title}>
          {res.title}
        </span>
        {res.fallback_fields.length > 0 && (
          <span
            title={`Matched on unmapped raw attribute keys: ${res.fallback_fields.join(", ")}. Map these fields (timeline field mappings or a ruleset fieldmap) if hits look wrong.`}
          >
            <AlertTriangle size={11} className="text-[var(--color-warning,#b58900)]" />
          </span>
        )}
        <Badge variant={STATUS_VARIANT[res.status] ?? "default"}>
          {res.status === "matched" ? `${res.match_count} hits` : res.status}
        </Badge>
        {res.sql && (
          <button
            className="font-mono text-[10px] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
            onClick={() => setShowSql((v) => !v)}
          >
            SQL
          </button>
        )}
        {res.status === "matched" && onTagFilter && (
          <button
            className="text-[var(--color-accent)] hover:opacity-80"
            title="Filter the grid to this rule's hits"
            onClick={() => onTagFilter(`sigma: ${res.title}`)}
          >
            <Filter size={12} />
          </button>
        )}
      </div>
      {res.error && (
        <p className="mt-0.5 pl-1 text-[10px] text-[var(--color-danger)]">{res.error}</p>
      )}
      {showSql && res.sql && (
        <pre className="mt-1 max-h-32 overflow-auto rounded bg-[var(--color-bg-elevated)] p-1.5 font-mono text-[10px] text-[var(--color-fg-secondary)]">
          {res.sql}
        </pre>
      )}
    </li>
  );
}
