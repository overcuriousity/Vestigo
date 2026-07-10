import { useMutation, useQueryClient } from "@tanstack/react-query";
import { anomaliesApi } from "@/api/anomalies";
import { dispositionsApi } from "@/api/dispositions";
import { shouldInvalidate } from "@/hooks/useCaseStream";
import { toast } from "@/stores/toasts";
import type { AnomaliesResponse, AnomalyFinding, DispositionKind } from "@/api/types";

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

/** Drop the findings a new normal/dismissed disposition suppresses. */
function filterFindings(data: AnomaliesResponse, t: DispositionTarget): AnomaliesResponse {
  const keep = (f: AnomalyFinding) => {
    if (t.field !== undefined && t.value !== undefined) {
      return !(
        (f.details as Record<string, unknown>)?.allowlist_field === t.field &&
        (f.details as Record<string, unknown>)?.allowlist_value === t.value
      );
    }
    return !(t.eventId && f.event_id === t.eventId);
  };
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
    mutationFn: async (t: DispositionTarget): Promise<void> => {
      if (t.kind === "confirmed") {
        if (!t.sourceId || !t.eventId) {
          throw new Error("Cannot confirm: no owning event for this finding.");
        }
        await anomaliesApi.persistFinding(caseId, t.sourceId, t.eventId, {
          detector: t.detector as Parameters<typeof anomaliesApi.persistFinding>[3]["detector"],
          content: t.content ?? "Manually confirmed finding",
          details: t.details ?? {},
        });
        return;
      }
      if (t.field !== undefined && t.value !== undefined) {
        await dispositionsApi.create(caseId, timelineId, {
          kind: t.kind,
          detector: t.detector,
          field: t.field,
          value: t.value,
        });
        return;
      }
      // Positional / value-less: event scope. Without an owning event there
      // is nothing to mark, so surface that rather than a false success.
      if (!t.sourceId || !t.eventId) {
        throw new Error("Cannot set disposition: no value key and no owning event.");
      }
      await dispositionsApi.create(caseId, timelineId, {
        kind: t.kind,
        detector: t.detector,
        source_id: t.sourceId,
        event_id: t.eventId,
      });
    },
    onMutate: async (t) => {
      if (t.kind === "confirmed") return { snapshots: [] };
      const prefix = ["anomalies", caseId, timelineId] as const;
      await qc.cancelQueries({ queryKey: prefix });
      const snapshots = qc.getQueriesData<AnomaliesResponse>({ queryKey: prefix });
      for (const [key, data] of snapshots) {
        if (!data) continue;
        // A detector-scoped disposition only suppresses that detector's
        // findings (backend matches `detector in (detector, "*")`); the
        // query key carries the detector id at index 3.
        if (t.detector !== "*" && key[3] !== t.detector) continue;
        qc.setQueryData(key, filterFindings(data, t));
      }
      return { snapshots };
    },
    onError: (_err, _t, ctx) => {
      // Roll the optimistically removed rows back; the global mutation error
      // toast (lib/queryClient.ts) reports why.
      for (const [key, data] of ctx?.snapshots ?? []) {
        if (data) qc.setQueryData(key, data);
      }
    },
    onSuccess: (_data, t) => {
      const label =
        t.field !== undefined && t.value !== undefined ? `${t.field}=${t.value}` : "event";
      toast.success(TOAST_BY_KIND[t.kind].title(label), TOAST_BY_KIND[t.kind].hint);
      qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
      qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
      if (t.kind === "confirmed") {
        qc.invalidateQueries({ queryKey: ["annotations"] });
      }
    },
    meta: { errorTitle: "Disposition failed" },
  });
}
