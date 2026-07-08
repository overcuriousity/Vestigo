/**
 * The two persistent-normality primitives (see docs/ANOMALY_DETECTION.md),
 * split into the pieces the Investigate panel places where they're needed:
 *
 *  - `BaselineSection` — baseline definitions (one baseline window + N suspect
 *    windows). Rendered inline in the Scope area so building/selecting the thing
 *    the `baseline` frame depends on is right where you pick that frame. Windows
 *    are set by typing UTC datetimes *or* arming a row and dragging the histogram.
 *  - `NormalValuesList` — the value-level allowlist, rendered at the bottom. Each
 *    entry shows its scope (`*` = all detectors, else the single detector it was
 *    marked from). Populated by the Normal action on findings / field rows.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Crosshair, Pencil, Plus, Trash2, X } from "lucide-react";
import { baselinesApi } from "@/api/baselines";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { BaselineDefinition } from "@/api/types";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
}

interface WinDraft {
  start: string;
  end: string;
}
interface SuspectDraft extends WinDraft {
  label: string;
}
interface Draft {
  name: string;
  baseline: WinDraft;
  suspects: SuspectDraft[];
}

/** Which draft row the next histogram brush fills. */
type Armed = { kind: "baseline" } | { kind: "suspect"; index: number } | null;

const EMPTY_DRAFT: Draft = { name: "", baseline: { start: "", end: "" }, suspects: [] };

// datetime-local <-> ISO. The typed wall-clock is interpreted as UTC (the whole
// UI shows UTC), so conversion is pure string slicing — no local-tz shift.
function toInput(iso: string): string {
  return iso ? iso.slice(0, 16) : "";
}
function fromInput(v: string): string {
  return v ? `${v}:00.000Z` : "";
}

/** Client mirror of the router's _validate_windows — returns human errors. */
function validate(draft: Draft): string[] {
  const errs: string[] = [];
  const b = draft.baseline;
  if (!b.start || !b.end) errs.push("Baseline needs a start and end.");
  else if (b.start >= b.end) errs.push("Baseline start must be before its end.");
  if (draft.suspects.length === 0) errs.push("Add at least one suspect window.");
  if (draft.suspects.length > 10) errs.push("At most 10 suspect windows.");
  const labels = new Set<string>();
  draft.suspects.forEach((s, i) => {
    const n = i + 1;
    if (!s.label.trim()) errs.push(`Suspect ${n} needs a label.`);
    else if (labels.has(s.label.trim())) errs.push(`Duplicate suspect label "${s.label.trim()}".`);
    labels.add(s.label.trim());
    if (!s.start || !s.end) errs.push(`Suspect ${n} needs a start and end.`);
    else if (s.start >= s.end) errs.push(`Suspect ${n} start must be before its end.`);
    // Half-open disjointness from the baseline.
    if (b.start && b.end && s.start && s.end && !(s.end <= b.start || s.start >= b.end)) {
      errs.push(`Suspect "${s.label.trim() || n}" overlaps the baseline window.`);
    }
  });
  return errs;
}

function draftFromDefinition(d: BaselineDefinition): Draft {
  return {
    name: d.name,
    baseline: { start: d.baseline.start, end: d.baseline.end },
    suspects: d.suspect_windows.map((w) => ({ label: w.label, start: w.start, end: w.end })),
  };
}

export function BaselineSection({ caseId, timelineId }: Props) {
  const qc = useQueryClient();
  const { activeBaselineId, setActiveBaselineId, markMode, setMarkMode, pendingRange, setPendingRange } =
    useBaselineStore();

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [armed, setArmed] = useState<Armed>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const definitions = data?.baselines ?? [];

  // A histogram brush lands in `pendingRange`; drop it into the armed row.
  useEffect(() => {
    if (!pendingRange || !armed) return;
    const win = { start: pendingRange.start, end: pendingRange.end };
    setDraft((d) => {
      if (armed.kind === "baseline") return { ...d, baseline: win };
      const suspects = d.suspects.map((s, i) => (i === armed.index ? { ...s, ...win } : s));
      return { ...d, suspects };
    });
    setPendingRange(null);
    setArmed(null);
  }, [pendingRange, armed, setPendingRange]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["baselines", caseId, timelineId] });

  const saveMut = useMutation({
    mutationFn: () => {
      const body = {
        name: draft.name.trim() || "Baseline",
        baseline_start: draft.baseline.start,
        baseline_end: draft.baseline.end,
        suspect_windows: draft.suspects.map((s) => ({ label: s.label.trim(), start: s.start, end: s.end })),
      };
      return editingId
        ? baselinesApi.update(caseId, timelineId, editingId, body)
        : baselinesApi.create(caseId, timelineId, body);
    },
    onSuccess: (res) => {
      resetDraft();
      setActiveBaselineId(res.baseline.id);
      invalidate();
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => baselinesApi.remove(caseId, timelineId, id),
    onSuccess: (_res, id) => {
      if (activeBaselineId === id) setActiveBaselineId(null);
      if (editingId === id) resetDraft();
      invalidate();
    },
  });

  const errors = useMemo(() => validate(draft), [draft]);
  const dirty = draft.baseline.start !== "" || draft.suspects.length > 0 || draft.name !== "";

  function resetDraft() {
    setDraft(EMPTY_DRAFT);
    setEditingId(null);
    setArmed(null);
    if (markMode) setMarkMode(false);
  }

  function armRow(target: Armed) {
    if (!markMode) setMarkMode(true);
    setArmed(target);
  }

  function addSuspect() {
    setDraft((d) => ({ ...d, suspects: [...d.suspects, { label: "", start: "", end: "" }] }));
  }

  return (
    <div className="space-y-4 text-sm">
      {/* ── Definition editor ─────────────────────────────────────────── */}
      <div className="space-y-2 rounded border border-[var(--color-border)] p-2.5">
        <div className="flex items-center gap-2">
          <span className="flex-1 text-xs font-semibold text-[var(--color-fg-secondary)]">
            {editingId ? "Edit definition" : "New definition"}
          </span>
          {dirty && (
            <button className="text-[10px] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]" onClick={resetDraft}>
              Reset
            </button>
          )}
        </div>
        {markMode && (
          <div className="rounded bg-[var(--color-accent-dim)] px-2 py-1 text-[11px] text-[var(--color-accent)]">
            {armed ? "Drag on the histogram to fill the highlighted row." : "Marking on — arm a row (crosshair) then drag the histogram."}
          </div>
        )}

        {/* Baseline row */}
        <WindowRow
          kind="baseline"
          label="Baseline"
          win={draft.baseline}
          armed={armed?.kind === "baseline"}
          onArm={() => armRow({ kind: "baseline" })}
          onChange={(w) => setDraft((d) => ({ ...d, baseline: { ...d.baseline, ...w } }))}
        />

        {/* Suspect rows */}
        {draft.suspects.map((s, i) => (
          <WindowRow
            key={i}
            kind="suspect"
            label={`Suspect ${i + 1}`}
            win={s}
            labelValue={s.label}
            armed={armed?.kind === "suspect" && armed.index === i}
            onArm={() => armRow({ kind: "suspect", index: i })}
            onLabelChange={(label) =>
              setDraft((d) => ({ ...d, suspects: d.suspects.map((x, j) => (j === i ? { ...x, label } : x)) }))
            }
            onChange={(w) =>
              setDraft((d) => ({ ...d, suspects: d.suspects.map((x, j) => (j === i ? { ...x, ...w } : x)) }))
            }
            onRemove={() => setDraft((d) => ({ ...d, suspects: d.suspects.filter((_, j) => j !== i) }))}
          />
        ))}

        <Button size="sm" variant="ghost" className="gap-1 text-xs" onClick={addSuspect}>
          <Plus size={11} /> Add suspect window
        </Button>

        {/* Name + save */}
        {dirty && (
          <div className="space-y-1.5 border-t border-[var(--color-border)] pt-2">
            <div className="flex items-center gap-1.5">
              <input
                value={draft.name}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                placeholder="definition name"
                className="h-6 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1.5 text-xs"
              />
              <Button
                size="sm"
                className="gap-1 text-xs"
                disabled={errors.length > 0 || saveMut.isPending}
                onClick={() => saveMut.mutate()}
              >
                {saveMut.isPending ? <Spinner size={11} /> : <Check size={11} />}
                {editingId ? "Update" : "Save"}
              </Button>
            </div>
            {saveMut.isError && (
              <div className="text-xs text-[var(--color-error)]">
                {(saveMut.error as Error)?.message ?? "Failed to save"}
              </div>
            )}
            {errors.length > 0 && (
              <ul className="space-y-0.5 text-[10px] text-[var(--color-fg-muted)]">
                {errors.map((e, i) => (
                  <li key={i}>· {e}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* ── Saved definitions ─────────────────────────────────────────── */}
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
            className={cn(
              "flex items-center gap-2 rounded border px-2 py-1.5 transition-colors",
              d.id === activeBaselineId
                ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]"
                : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
            )}
          >
            <button
              className="min-w-0 flex-1 text-left"
              onClick={() => setActiveBaselineId(d.id === activeBaselineId ? null : d.id)}
              title={d.id === activeBaselineId ? "Active — click to deactivate" : "Activate this baseline"}
            >
              <div className="flex items-center gap-1 truncate text-xs font-medium text-[var(--color-fg-primary)]">
                {d.id === activeBaselineId && <Check size={11} className="text-[var(--color-accent)]" />}
                {d.name}
              </div>
              <div className="text-[10px] text-[var(--color-fg-muted)]">
                {d.suspect_windows.length} suspect window{d.suspect_windows.length === 1 ? "" : "s"}
              </div>
            </button>
            <button
              className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
              onClick={() => {
                setDraft(draftFromDefinition(d));
                setEditingId(d.id);
              }}
              title="Edit"
            >
              <Pencil size={12} />
            </button>
            <button
              className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
              onClick={() => deleteMut.mutate(d.id)}
              title="Delete definition"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The value-level allowlist ("Normal values"), rendered at the panel bottom. */
export function NormalValuesList({ caseId, timelineId }: Props) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["allowlist", caseId, timelineId],
    queryFn: () => baselinesApi.listAllowlist(caseId, timelineId),
  });
  const allowlist = data?.entries ?? [];
  const removeMut = useMutation({
    mutationFn: (id: string) => baselinesApi.removeAllowlist(caseId, timelineId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["allowlist", caseId, timelineId] }),
  });

  return (
    <div className="space-y-1 text-sm">
      <p className="text-[10px] text-[var(--color-fg-muted)]">
        Values marked normal are hidden from future scans — the manual extension of your baseline.
      </p>
      {allowlist.length === 0 && (
        <div className="text-xs text-[var(--color-fg-muted)]">
          None yet. Use <strong>Normal</strong> on a finding or a field value.
        </div>
      )}
      {allowlist.map((e) => (
        <div
          key={e.id}
          className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-1 text-xs"
        >
          <span className="min-w-0 flex-1 truncate">
            <span className="text-[var(--color-fg-muted)]">
              {e.detector === "*" ? "all detectors" : e.detector} · {e.field}:
            </span>{" "}
            <span className="text-[var(--color-fg-primary)]">{e.value}</span>
          </span>
          <button
            className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
            onClick={() => removeMut.mutate(e.id)}
            title="Remove — value becomes flaggable again"
          >
            <Trash2 size={11} />
          </button>
        </div>
      ))}
    </div>
  );
}

function WindowRow({
  label,
  labelValue,
  win,
  armed,
  onArm,
  onChange,
  onLabelChange,
  onRemove,
}: {
  kind: "baseline" | "suspect";
  label: string;
  labelValue?: string;
  win: WinDraft;
  armed: boolean;
  onArm: () => void;
  onChange: (w: Partial<WinDraft>) => void;
  onLabelChange?: (label: string) => void;
  onRemove?: () => void;
}) {
  return (
    <div
      className={cn(
        "space-y-1 rounded border p-1.5",
        armed ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]" : "border-[var(--color-border-subtle)]",
      )}
    >
      <div className="flex items-center gap-1.5">
        <span className="w-16 shrink-0 text-[11px] font-medium text-[var(--color-fg-secondary)]">{label}</span>
        {onLabelChange !== undefined ? (
          <input
            value={labelValue ?? ""}
            onChange={(e) => onLabelChange(e.target.value)}
            placeholder="label"
            className="h-6 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1.5 text-xs"
          />
        ) : (
          <span className="flex-1" />
        )}
        <button
          onClick={onArm}
          title="Arm — then drag on the histogram to set this range"
          className={cn(
            "rounded p-0.5",
            armed
              ? "text-[var(--color-accent)]"
              : "text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]",
          )}
        >
          <Crosshair size={12} />
        </button>
        {onRemove && (
          <button
            onClick={onRemove}
            className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
            title="Remove"
          >
            <X size={12} />
          </button>
        )}
      </div>
      <div className="flex items-center gap-1 pl-[4.25rem]">
        <input
          type="datetime-local"
          value={toInput(win.start)}
          onChange={(e) => onChange({ start: fromInput(e.target.value) })}
          className="h-6 min-w-0 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1 text-[11px]"
        />
        <span className="text-[10px] text-[var(--color-fg-muted)]">→</span>
        <input
          type="datetime-local"
          value={toInput(win.end)}
          onChange={(e) => onChange({ end: fromInput(e.target.value) })}
          className="h-6 min-w-0 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1 text-[11px]"
        />
        <span className="text-[9px] uppercase tracking-wide text-[var(--color-fg-muted)]">utc</span>
      </div>
    </div>
  );
}
