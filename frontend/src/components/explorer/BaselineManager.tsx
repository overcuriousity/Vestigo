/**
 * BaselineManager — create/select/delete baseline definitions (a baseline
 * range + N suspect windows) and manage the detector value-allowlist for a
 * timeline.
 *
 * Baseline windows are the time-based normality signal for temporal anomaly
 * detection; the allowlist is the value-based one (see docs/ANOMALY_DETECTION).
 * Ranges are marked on the histogram in "mark" mode: a brushed range lands in
 * the store as `pendingRange`, and this panel turns it into the baseline or a
 * labeled suspect window.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Plus, Trash2, X } from "lucide-react";
import { baselinesApi } from "@/api/baselines";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { SuspectWindow } from "@/api/types";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onClose: () => void;
}

/** A baseline range + suspect windows being assembled before it's saved. */
interface Draft {
  baseline: { start: string; end: string } | null;
  suspects: SuspectWindow[];
}

export function BaselineManager({ caseId, timelineId, onClose }: Props) {
  const qc = useQueryClient();
  const { activeBaselineId, setActiveBaselineId, markMode, setMarkMode, pendingRange, setPendingRange } =
    useBaselineStore();

  const [draft, setDraft] = useState<Draft>({ baseline: null, suspects: [] });
  const [name, setName] = useState("");
  const [suspectLabel, setSuspectLabel] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const definitions = data?.baselines ?? [];

  const { data: allowData } = useQuery({
    queryKey: ["allowlist", caseId, timelineId],
    queryFn: () => baselinesApi.listAllowlist(caseId, timelineId),
  });
  const allowlist = allowData?.entries ?? [];

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["baselines", caseId, timelineId] });

  const createMut = useMutation({
    mutationFn: () => {
      if (!draft.baseline) throw new Error("Mark a baseline range first");
      return baselinesApi.create(caseId, timelineId, {
        name: name.trim() || "Baseline",
        baseline_start: draft.baseline.start,
        baseline_end: draft.baseline.end,
        suspect_windows: draft.suspects.map((w) => ({
          label: w.label,
          start: w.start,
          end: w.end,
        })),
      });
    },
    onSuccess: (res) => {
      setDraft({ baseline: null, suspects: [] });
      setName("");
      setActiveBaselineId(res.baseline.id);
      invalidate();
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => baselinesApi.remove(caseId, timelineId, id),
    onSuccess: (_res, id) => {
      if (activeBaselineId === id) setActiveBaselineId(null);
      invalidate();
    },
  });

  const removeAllowMut = useMutation({
    mutationFn: (id: string) => baselinesApi.removeAllowlist(caseId, timelineId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["allowlist", caseId, timelineId] }),
  });

  const draftValid = draft.baseline !== null && draft.suspects.length > 0;

  const pendingActions = useMemo(() => {
    if (!pendingRange) return null;
    return (
      <div className="rounded border border-[var(--color-warning)]/50 bg-[var(--color-warning)]/5 p-2 space-y-2">
        <div className="text-xs text-[var(--color-fg-secondary)]">
          Marked {fmtTs(pendingRange.start)} → {fmtTs(pendingRange.end)}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            className="text-xs"
            onClick={() => {
              setDraft((d) => ({ ...d, baseline: { ...pendingRange } }));
              setPendingRange(null);
            }}
          >
            Set as baseline
          </Button>
          <input
            value={suspectLabel}
            onChange={(e) => setSuspectLabel(e.target.value)}
            placeholder="window label"
            className="h-6 w-28 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1.5 text-xs"
          />
          <Button
            size="sm"
            variant="ghost"
            className="text-xs"
            disabled={!suspectLabel.trim()}
            onClick={() => {
              setDraft((d) => ({
                ...d,
                suspects: [
                  ...d.suspects,
                  {
                    label: suspectLabel.trim(),
                    start: pendingRange.start,
                    end: pendingRange.end,
                  },
                ],
              }));
              setSuspectLabel("");
              setPendingRange(null);
            }}
          >
            Add suspect window
          </Button>
          <button
            className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]"
            onClick={() => setPendingRange(null)}
            title="Discard"
          >
            <X size={12} />
          </button>
        </div>
      </div>
    );
  }, [pendingRange, suspectLabel, setPendingRange]);

  return (
    <div className="flex h-full flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]" style={{ width: 320 }}>
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">Baselines</h3>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X size={14} />
        </Button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-3 text-sm">
        {/* Mark-mode hint / toggle */}
        <div className="flex items-center justify-between rounded bg-[var(--color-bg-base)] px-2 py-1.5">
          <span className="text-xs text-[var(--color-fg-muted)]">
            {markMode ? "Drag on the histogram to mark a range" : "Enable marking to build a baseline"}
          </span>
          <Button size="sm" variant={markMode ? "default" : "ghost"} className="text-xs" onClick={() => setMarkMode(!markMode)}>
            {markMode ? "Marking" : "Mark"}
          </Button>
        </div>

        {pendingActions}

        {/* Draft under construction */}
        {(draft.baseline || draft.suspects.length > 0) && (
          <div className="space-y-1.5 rounded border border-[var(--color-border)] p-2">
            <div className="text-xs font-semibold text-[var(--color-fg-secondary)]">New definition</div>
            {draft.baseline && (
              <div className="text-xs text-[var(--color-info)]">
                Baseline: {fmtTs(draft.baseline.start)} → {fmtTs(draft.baseline.end)}
              </div>
            )}
            {draft.suspects.map((w, i) => (
              <div key={i} className="flex items-center justify-between text-xs text-[var(--color-warning)]">
                <span>
                  {w.label}: {fmtTs(w.start)} → {fmtTs(w.end)}
                </span>
                <button
                  className="text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
                  onClick={() =>
                    setDraft((d) => ({ ...d, suspects: d.suspects.filter((_, j) => j !== i) }))
                  }
                >
                  <X size={11} />
                </button>
              </div>
            ))}
            <div className="flex items-center gap-1.5 pt-1">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="definition name"
                className="h-6 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1.5 text-xs"
              />
              <Button
                size="sm"
                className="gap-1 text-xs"
                disabled={!draftValid || createMut.isPending}
                onClick={() => createMut.mutate()}
              >
                {createMut.isPending ? <Spinner size={11} /> : <Plus size={11} />}
                Save
              </Button>
            </div>
            {createMut.isError && (
              <div className="text-xs text-[var(--color-error)]">
                {(createMut.error as Error)?.message ?? "Failed to save"}
              </div>
            )}
            {!draftValid && (
              <div className="text-[10px] text-[var(--color-fg-muted)]">
                Needs a baseline range and at least one suspect window.
              </div>
            )}
          </div>
        )}

        {/* Saved definitions */}
        <div className="space-y-1">
          <div className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
            Saved definitions
          </div>
          {isLoading && <Spinner size={12} />}
          {!isLoading && definitions.length === 0 && (
            <div className="text-xs text-[var(--color-fg-muted)]">None yet.</div>
          )}
          {definitions.map((d) => (
            <div
              key={d.id}
              className={cnActive(d.id === activeBaselineId)}
              onClick={() => setActiveBaselineId(d.id === activeBaselineId ? null : d.id)}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1 truncate text-xs font-medium text-[var(--color-fg-primary)]">
                  {d.id === activeBaselineId && <Check size={11} className="text-[var(--color-accent)]" />}
                  {d.name}
                </div>
                <div className="text-[10px] text-[var(--color-fg-muted)]">
                  {d.suspect_windows.length} suspect window{d.suspect_windows.length === 1 ? "" : "s"}
                </div>
              </div>
              <button
                className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
                onClick={(e) => {
                  e.stopPropagation();
                  deleteMut.mutate(d.id);
                }}
                title="Delete definition"
              >
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>

        {/* Allowlisted values */}
        <div className="space-y-1">
          <div className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
            Allowlisted values
          </div>
          {allowlist.length === 0 && (
            <div className="text-xs text-[var(--color-fg-muted)]">
              Values you mark "normal" on a finding appear here.
            </div>
          )}
          {allowlist.map((e) => (
            <div
              key={e.id}
              className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-1 text-xs"
            >
              <span className="min-w-0 flex-1 truncate">
                <span className="text-[var(--color-fg-muted)]">{e.detector} · {e.field}</span>{" "}
                <span className="text-[var(--color-fg-primary)]">{e.value}</span>
              </span>
              <button
                className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
                onClick={() => removeAllowMut.mutate(e.id)}
                title="Remove — value becomes flaggable again"
              >
                <Trash2 size={11} />
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function cnActive(active: boolean): string {
  return [
    "flex cursor-pointer items-center gap-2 rounded border px-2 py-1.5 transition-colors",
    active
      ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]"
      : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
  ].join(" ");
}
