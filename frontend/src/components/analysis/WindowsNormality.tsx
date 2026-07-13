/**
 * The two persistent-normality primitives (see docs/ANOMALY_DETECTION.md),
 * split into the pieces the Investigate panel places where they're needed:
 *
 *  - `BaselineSection` — baseline definitions (one baseline window + N suspect
 *    windows). Rendered inline in the Scope area so building/selecting the thing
 *    the `baseline` frame depends on is right where you pick that frame. Windows
 *    are set by typing UTC datetimes *or* arming a row and dragging the histogram.
 *  - `NormalValuesList` — the disposition list (normal / dismissed / confirmed),
 *    rendered at the bottom, grouped by verdict. Each entry shows its scope
 *    (`*` = all detectors, else the single detector it was marked from; a
 *    field:value pair or a single event). Populated by the Normal / Dismiss /
 *    Confirm actions on findings and field rows.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Crosshair, Pencil, Plus, Trash2, X } from "lucide-react";
import { baselinesApi } from "@/api/baselines";
import { dispositionsApi } from "@/api/dispositions";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { DateTimeField } from "@/components/ui/DateTimeField";
import { InfoHint } from "@/components/ui/InfoHint";
import { useBaselineStore, type ArmedTarget } from "@/stores/baseline";
import type { BaselineDefinition, DispositionKind } from "@/api/types";
import { cn } from "@/lib/cn";
import { GLOSSARY } from "@/lib/glossary";

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

/** Which draft row the next histogram brush fills — see ArmedTarget in
 * stores/baseline.ts; it lives in the store so the drawer can hide while a
 * brush is awaited without this component unmounting and losing it. */

const EMPTY_DRAFT: Draft = { name: "", baseline: { start: "", end: "" }, suspects: [] };

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
  const { activeBaselineId, setActiveBaselineId, markMode, setMarkMode, pendingRange, armed, setArmed } =
    useBaselineStore();

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  // The definition builder is a big form; once the analyst has saved baselines
  // it collapses behind a "+ New definition" button so saved defs lead. It
  // stays open while there's nothing saved yet, or whenever a build is active.
  const [builderOpen, setBuilderOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const definitions = data?.baselines ?? [];

  // A histogram brush lands in `pendingRange`; drop it into the armed row.
  // A brush with no armed row (mark mode toggled directly on the histogram)
  // defaults to the baseline window — the most common thing to mark. Turning
  // mark mode off (which also clears pendingRange/armed in the store) lets
  // the builder drawer reappear with the filled row.
  useEffect(() => {
    if (!pendingRange) return;
    const target = armed ?? { kind: "baseline" as const };
    const win = { start: pendingRange.start, end: pendingRange.end };
    setDraft((d) => {
      if (target.kind === "baseline") return { ...d, baseline: win };
      const suspects = d.suspects.map((s, i) => (i === target.index ? { ...s, ...win } : s));
      return { ...d, suspects };
    });
    setMarkMode(false);
  }, [pendingRange, armed, setMarkMode]);

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
    // Save errors render inline under the builder — skip the global toast.
    meta: { silentError: true },
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

  // Show the builder form when explicitly opened, mid-edit/mid-build, or when
  // there's nothing saved yet to lead with.
  const showBuilder =
    builderOpen || editingId !== null || dirty || markMode || definitions.length === 0;

  function resetDraft() {
    setDraft(EMPTY_DRAFT);
    setEditingId(null);
    setArmed(null);
    setBuilderOpen(false);
    if (markMode) setMarkMode(false);
  }

  function armRow(target: ArmedTarget) {
    if (!markMode) setMarkMode(true);
    setArmed(target);
  }

  function addSuspect() {
    setDraft((d) => ({ ...d, suspects: [...d.suspects, { label: "", start: "", end: "" }] }));
  }

  return (
    <div className="space-y-4 text-sm">
      {/* ── Definition editor ─────────────────────────────────────────── */}
      {!showBuilder ? (
        <Button
          size="sm"
          variant="ghost"
          className="w-full justify-center gap-1 text-xs"
          onClick={() => setBuilderOpen(true)}
        >
          <Plus size={12} /> New definition
        </Button>
      ) : (
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
        {/* No inline mark-mode hint here: while mark mode is on, the builder
            drawer hides itself and the floating pill (BaselineBuilderDrawer)
            carries the "drag on the histogram" guidance instead. */}

        {/* Baseline row */}
        <WindowRow
          kind="baseline"
          label="Baseline"
          hint={GLOSSARY.baseline}
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
            hint={i === 0 ? GLOSSARY.suspectWindow : undefined}
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
                placeholder='Name this baseline — e.g. "known-good week"'
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
      )}

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

const KIND_META: Record<
  DispositionKind,
  { heading: string; hint: string; removeTitle: string }
> = {
  normal: {
    heading: "Normal",
    hint: "Expected behavior — extends the baseline; hidden from future scans.",
    removeTitle: "Remove — becomes flaggable again",
  },
  dismissed: {
    heading: "Dismissed",
    hint: "Noise for this investigation — hidden from view, detectors keep scoring.",
    removeTitle: "Remove — becomes visible again",
  },
  confirmed: {
    heading: "Confirmed",
    hint: "Escalated findings — survive detector re-runs.",
    removeTitle: "Remove — no longer protected across re-runs",
  },
  routine: {
    heading: "Routine",
    hint: "Recurring expected patterns (Patterns tab) — collapsible in the event grid.",
    removeTitle: "Remove — its events reappear in the grid",
  },
};

/** The analyst's dispositions (normal / dismissed / confirmed), grouped by verdict. */
export function NormalValuesList({ caseId, timelineId }: Props) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["dispositions", caseId, timelineId],
    queryFn: () => dispositionsApi.list(caseId, timelineId),
  });
  const rows = data?.dispositions ?? [];
  const removeMut = useMutation({
    mutationFn: (id: string) => dispositionsApi.remove(caseId, timelineId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
      // Un-suppressing must let findings come back — re-run the detector
      // queries (adding a disposition never needs this: useDisposition
      // filters cached results optimistically instead).
      qc.invalidateQueries({ queryKey: ["anomalies", caseId, timelineId] });
    },
    meta: { successToast: "Disposition removed" },
  });

  const kinds: DispositionKind[] = ["normal", "dismissed", "confirmed"];
  return (
    <div className="space-y-2 text-sm">
      <p className="text-[10px] text-[var(--color-fg-muted)]">
        Your verdicts on findings. <strong>Normal</strong> extends the baseline,{" "}
        <strong>Dismissed</strong> hides noise without changing detection,{" "}
        <strong>Confirmed</strong> pins escalated findings.
      </p>
      {rows.length === 0 && (
        <div className="text-xs text-[var(--color-fg-muted)]">
          None yet. Use <strong>Normal</strong>, <strong>Dismiss</strong> or{" "}
          <strong>Confirm</strong> on a finding or a field value.
        </div>
      )}
      {kinds.map((kind) => {
        const group = rows.filter((d) => d.kind === kind);
        if (group.length === 0) return null;
        return (
          <div key={kind} className="space-y-1">
            <div
              className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]"
              title={KIND_META[kind].hint}
            >
              {KIND_META[kind].heading} ({group.length})
            </div>
            {group.map((d) => (
              <div
                key={d.id}
                className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-1 text-xs"
              >
                <span className="min-w-0 flex-1 truncate" title={d.note ?? undefined}>
                  <span className="text-[var(--color-fg-muted)]">
                    {d.detector === "*" ? "all detectors" : d.detector} ·{" "}
                    {d.field !== null ? `${d.field}:` : "event"}
                  </span>{" "}
                  <span className="text-[var(--color-fg-primary)]">
                    {d.field !== null ? d.value : d.event_id}
                  </span>
                </span>
                <button
                  className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-error)]"
                  onClick={() => removeMut.mutate(d.id)}
                  title={KIND_META[kind].removeTitle}
                >
                  <Trash2 size={11} />
                </button>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function WindowRow({
  label,
  hint,
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
  hint?: string;
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
        <span className="flex w-16 shrink-0 items-center gap-1 text-[11px] font-medium text-[var(--color-fg-secondary)]">
          {label}
          {hint && <InfoHint content={hint} size={11} />}
        </span>
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
        <DateTimeField
          className="min-w-0 flex-1"
          value={win.start}
          onChange={(iso) => onChange({ start: iso ?? "" })}
          placeholder="start"
          ariaLabel={`${label} start (UTC)`}
        />
        <span className="text-[10px] text-[var(--color-fg-muted)]">→</span>
        <DateTimeField
          className="min-w-0 flex-1"
          value={win.end}
          onChange={(iso) => onChange({ end: iso ?? "" })}
          placeholder="end"
          ariaLabel={`${label} end (UTC)`}
        />
      </div>
    </div>
  );
}
