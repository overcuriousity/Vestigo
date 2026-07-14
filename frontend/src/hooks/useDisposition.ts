import { useMutation, useQueryClient } from "@tanstack/react-query";
import { anomaliesApi } from "@/api/anomalies";
import { SHOW_DISMISSED_KEY } from "@/components/analysis/detector-hooks";
import { DETECTORS, type DetectorId } from "@/components/analysis/detector-registry";
import { dispositionsApi } from "@/api/dispositions";
import { shouldInvalidate } from "@/hooks/useCaseStream";
import { toast } from "@/stores/toasts";
import type {
  AnomaliesResponse,
  AnomalyFinding,
  Disposition,
  DispositionKind,
  DispositionListResponse,
} from "@/api/types";

/** Shape of the shared detector-sweep cache (see useDetectorSweep). */
type SweepData = Record<DetectorId, AnomaliesResponse | null>;

/** What a single disposition action needs to resolve its target finding. */
export interface DispositionTarget {
  /** Analyst verdict — see docs/ANOMALY_DETECTION.md for the taxonomy. */
  kind: DispositionKind;
  /**
   * Detector the verdict is scoped to. `"*"` = detector-agnostic (all value
   * detectors), written from a field-value row where there is no detector
   * context. A concrete detector id scopes it to that detector, written from
   * a finding row. `confirmed` requires a concrete detector.
   */
  detector: string;
  /** Value scope. Omit for positional findings (timestamp_order). */
  field?: string;
  value?: string;
  /** Event scope — required for positional/value-less findings and `confirmed`. */
  sourceId?: string;
  eventId?: string;
  /** confirmed only: human-readable finding text + structured details snapshot. */
  content?: string;
  details?: Record<string, unknown>;
}

/** True when the disposition target covers this finding. Sibling of
 * `dispositionCoversFinding` in lib/triage-coverage.ts, which matches
 * persisted Disposition rows for the coverage badges. */
function matchesTarget(f: AnomalyFinding, t: DispositionTarget): boolean {
  if (t.field !== undefined && t.value !== undefined) {
    return (
      (f.details as Record<string, unknown>)?.allowlist_field === t.field &&
      (f.details as Record<string, unknown>)?.allowlist_value === t.value
    );
  }
  return !!t.eventId && f.event_id === t.eventId;
}

/** Drop the findings a new normal/dismissed disposition suppresses. */
function filterFindings(data: AnomaliesResponse, t: DispositionTarget): AnomaliesResponse {
  const keep = (f: AnomalyFinding) => !matchesTarget(f, t);
  const results = data.results.filter(keep);
  const dropped = data.results.length - results.length;
  return {
    ...data,
    results,
    // Keep the "N of M findings" bar consistent with the removal.
    total_findings:
      data.total_findings !== undefined
        ? Math.max(0, data.total_findings - dropped)
        : undefined,
    // Dismissed findings stay counted — the backend reports them the same way.
    dismissed_count:
      t.kind === "dismissed" ? (data.dismissed_count ?? 0) + dropped : data.dismissed_count,
  };
}

/**
 * Flag (not drop) the findings a new dismissal covers — for caches fetched
 * with `include_dismissed` (the show-dismissed toggle), where dismissed rows
 * stay visible, dimmed. `total_findings` is untouched: the backend keeps
 * dismissed findings counted there when they stay in `results`.
 */
function markFindingsDismissed(data: AnomaliesResponse, t: DispositionTarget): AnomaliesResponse {
  let flagged = 0;
  const results = data.results.map((f) => {
    if (!matchesTarget(f, t) || f.dismissed) return f;
    flagged += 1;
    return { ...f, dismissed: true };
  });
  return {
    ...data,
    results,
    dismissed_count: (data.dismissed_count ?? 0) + flagged,
  };
}

/** Flag (not drop) the findings a new confirmation covers — the row stays
 * visible with a durable confirmed badge, matching what a refetch returns
 * (the backend stamps `confirmed: true` on covered findings). */
function markFindingsConfirmed(data: AnomaliesResponse, t: DispositionTarget): AnomaliesResponse {
  let changed = false;
  const results = data.results.map((f) => {
    if (!matchesTarget(f, t) || f.confirmed) return f;
    changed = true;
    return { ...f, confirmed: true };
  });
  return changed ? { ...data, results } : data;
}

const TOAST_BY_KIND: Record<DispositionKind, { title: (label: string) => string; hint: string }> = {
  normal: {
    title: (label) => `Marked normal — ${label}`,
    hint: "No longer flagged; the baseline now includes it. Manage under Windows & normality.",
  },
  dismissed: {
    title: (label) => `Dismissed — ${label}`,
    hint: "Hidden as noise; detectors keep scoring it. Manage under Windows & normality.",
  },
  confirmed: {
    title: (label) => `Confirmed — ${label}`,
    hint: "Escalated as a durable finding; it survives detector re-runs.",
  },
  routine: {
    title: (label) => `Marked routine — ${label}`,
    hint: "Recurring expected pattern; its occurrences can be collapsed in the event grid.",
  },
};

/**
 * Declare a disposition on a finding — the single mutation behind the
 * Normal / Dismiss / Confirm row actions (see docs/ANOMALY_DETECTION.md):
 *
 * - `normal` extends the baseline: value-shaped targets become a value-scoped
 *   disposition (suppressed on every event), positional ones an event-scoped
 *   one. Suppression is a post-detection filter on the backend, so the same
 *   filter is applied *optimistically* to every cached anomalies result here —
 *   the row disappears on click instead of after a re-scan.
 * - `dismissed` is presentation-only noise triage — same optimistic removal,
 *   but the backend keeps scoring and reports a `dismissed_count`.
 * - `confirmed` escalates: it calls the persist endpoint (which writes the
 *   system annotation + confirmed disposition in one audited action) and
 *   leaves the row visible.
 *
 * Failure rolls the optimistic removal back and surfaces the error via the
 * global mutation error toast. Detector re-runs pick dispositions up
 * server-side, so no blanket `["anomalies"]` invalidation is needed (sweep
 * counts may lag until their next refresh — a triage hint, not the authority).
 */
export function useDisposition(caseId: string, timelineId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (
      t: DispositionTarget,
    ): Promise<{ dispositionId?: string; materializationJobId?: string }> => {
      if (t.kind === "confirmed") {
        if (!t.sourceId || !t.eventId) {
          throw new Error("Cannot confirm: no owning event for this finding.");
        }
        await anomaliesApi.persistFinding(caseId, t.sourceId, t.eventId, {
          detector: t.detector as Parameters<typeof anomaliesApi.persistFinding>[3]["detector"],
          content: t.content ?? "Manually confirmed finding",
          details: t.details ?? {},
        });
        return {};
      }
      if (t.field !== undefined && t.value !== undefined) {
        const res = await dispositionsApi.create(caseId, timelineId, {
          kind: t.kind,
          detector: t.detector,
          field: t.field,
          value: t.value,
          // routine needs the motif snapshot (details.values drives the
          // occurrence materialization server-side).
          details: t.kind === "routine" ? (t.details ?? null) : undefined,
        });
        return {
          dispositionId: res.disposition.id,
          materializationJobId: res.materialization_job_id,
        };
      }
      // Positional / value-less: event scope. Without an owning event there
      // is nothing to mark, so surface that rather than a false success.
      if (!t.sourceId || !t.eventId) {
        throw new Error("Cannot set disposition: no value key and no owning event.");
      }
      const res = await dispositionsApi.create(caseId, timelineId, {
        kind: t.kind,
        detector: t.detector,
        source_id: t.sourceId,
        event_id: t.eventId,
      });
      return { dispositionId: res.disposition.id };
    },
    onMutate: async (t) => {
      // routine leaves the findings list untouched — instead, optimistically
      // append a row to the routine-dispositions cache so the motif dims on
      // click (PatternsView derives `isRoutine` from that cache).
      if (t.kind === "routine") {
        const routineKey = ["dispositions", caseId, timelineId, "routine"] as const;
        await qc.cancelQueries({ queryKey: routineKey });
        const routineSnapshot = qc.getQueryData<DispositionListResponse>(routineKey);
        if (routineSnapshot && t.field !== undefined && t.value !== undefined) {
          const optimistic: Disposition = {
            id: `optimistic-${t.field}-${t.value}`,
            case_id: caseId,
            timeline_id: timelineId,
            kind: "routine",
            detector: t.detector,
            field: t.field,
            value: t.value,
            source_id: null,
            event_id: null,
            note: null,
            details: (t.details as Disposition["details"]) ?? null,
            created_by: null,
            created_at: null,
          };
          qc.setQueryData(routineKey, {
            ...routineSnapshot,
            dispositions: [optimistic, ...routineSnapshot.dispositions],
          });
        }
        return { snapshots: [], sweepSnapshots: [], routineSnapshot };
      }
      const prefix = ["anomalies", caseId, timelineId] as const;
      await qc.cancelQueries({ queryKey: prefix });
      const snapshots = qc.getQueriesData<AnomaliesResponse>({ queryKey: prefix });
      for (const [key, data] of snapshots) {
        if (!data) continue;
        // The ["anomalies", case, timeline] prefix also holds non-findings
        // caches (the field-inventory queries keyed "fields"/"numeric-fields")
        // whose payload has no `results` array. A detector-agnostic
        // disposition (detector "*") skips the detector-id guard below, so
        // without this shape check filterFindings would crash on them —
        // killing the whole mutation before the API call.
        if (!Array.isArray(data.results)) continue;
        // A detector-scoped disposition only suppresses that detector's
        // findings (backend matches `detector in (detector, "*")`); the
        // query key carries the detector id at index 3.
        if (t.detector !== "*" && key[3] !== t.detector) continue;
        // Caches fetched with the show-dismissed toggle carry the named
        // "dismissed-shown" key segment (see useShowDismissed): there, a
        // dismissal keeps the row visible, flagged + dimmed, matching what a
        // refetch returns. Normal still removes — the backend suppresses it
        // either way. Confirmed never removes: it flags the row so it renders
        // its durable confirmed badge immediately.
        const showsDismissed = key.includes(SHOW_DISMISSED_KEY);
        qc.setQueryData(
          key,
          t.kind === "confirmed"
            ? markFindingsConfirmed(data, t)
            : t.kind === "dismissed" && showsDismissed
              ? markFindingsDismissed(data, t)
              : filterFindings(data, t),
        );
      }
      // The shared detector sweep (feeding FindingsFeed and the accordion's
      // badges) lives under its own key, not the ["anomalies", …] prefix —
      // without filtering it too, a verdict declared from the feed leaves the
      // row visibly untouched and reads as a dead button. Sweeps are always
      // fetched without include_dismissed, so plain removal matches a refetch.
      const sweepPrefix = ["detector-sweep-v2", caseId, timelineId] as const;
      await qc.cancelQueries({ queryKey: sweepPrefix });
      const sweepSnapshots = qc.getQueriesData<SweepData>({ queryKey: sweepPrefix });
      for (const [key, data] of sweepSnapshots) {
        if (!data) continue;
        let changed = false;
        const next = { ...data };
        for (const meta of DETECTORS) {
          const response = next[meta.id];
          if (!response) continue;
          if (t.detector !== "*" && meta.detector !== t.detector) continue;
          const updated =
            t.kind === "confirmed"
              ? markFindingsConfirmed(response, t)
              : filterFindings(response, t);
          if (updated !== response && (t.kind === "confirmed" || updated.results.length !== response.results.length)) {
            next[meta.id] = updated;
            changed = true;
          }
        }
        if (changed) qc.setQueryData(key, next);
      }
      return { snapshots, sweepSnapshots, routineSnapshot: undefined };
    },
    onError: (_err, t, ctx) => {
      // Roll the optimistically removed rows back; the global mutation error
      // toast (lib/queryClient.ts) reports why.
      for (const [key, data] of ctx?.snapshots ?? []) {
        if (data) qc.setQueryData(key, data);
      }
      for (const [key, data] of ctx?.sweepSnapshots ?? []) {
        if (data) qc.setQueryData(key, data);
      }
      if (t.kind === "routine" && ctx?.routineSnapshot) {
        qc.setQueryData(["dispositions", caseId, timelineId, "routine"], ctx.routineSnapshot);
      }
    },
    onSuccess: (data, t) => {
      const label =
        t.field !== undefined && t.value !== undefined ? `${t.field}=${t.value}` : "event";
      const invalidate = () => {
        qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
        qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
      };
      // Undo deletes the just-created disposition — the finding resurfaces on
      // the next refetch. Confirm has no undo here: it also persisted a system
      // annotation, so undoing it is a deliberate act in the dispositions list.
      const undo =
        data.dispositionId !== undefined
          ? {
              label: "Undo",
              onClick: () => {
                void dispositionsApi.remove(caseId, timelineId, data.dispositionId!).then(() => {
                  invalidate();
                  qc.invalidateQueries({ queryKey: ["events"] });
                  toast.info(`Undone — ${label}`, "The verdict was removed.");
                });
              },
            }
          : undefined;
      toast.success(TOAST_BY_KIND[t.kind].title(label), TOAST_BY_KIND[t.kind].hint, undo);
      invalidate();
      if (t.kind === "confirmed") {
        qc.invalidateQueries({ queryKey: ["annotations"] });
      }
    },
    meta: { errorTitle: "Disposition failed" },
  });
}
