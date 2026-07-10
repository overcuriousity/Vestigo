import { useMutation, useQueryClient } from "@tanstack/react-query";
import { annotationsApi } from "@/api/annotations";
import { baselinesApi } from "@/api/baselines";
import { shouldInvalidate } from "@/hooks/useCaseStream";
import { toast } from "@/stores/toasts";
import type { AnomaliesResponse, AnomalyFinding } from "@/api/types";

/** What a single "mark normal" action needs to resolve its target. */
export interface MarkNormalTarget {
  /**
   * Detector the entry is scoped to. `"*"` = detector-agnostic (all value
   * detectors), written from a field-value row where there is no detector
   * context. A concrete detector id scopes it to that detector, written from a
   * finding row.
   */
  detector: string;
  /** Allowlist key. Omit for positional findings (timestamp_order). */
  field?: string;
  value?: string;
  /** Needed only for the per-event fallback (positional / value-less findings). */
  sourceId?: string;
  eventId?: string;
}

/** Drop the findings a new allowlist entry / normal annotation suppresses. */
function filterFindings(data: AnomaliesResponse, t: MarkNormalTarget): AnomaliesResponse {
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
  };
}

/**
 * Declare a value normal so detectors stop flagging it — the manual extension
 * of the baseline window (see docs/ANOMALY_DETECTION.md). Value-shaped targets
 * become a `(detector, field, value)` allowlist entry (value-level: suppressed
 * on every event); positional ones (timestamp_order, or any target missing a
 * field/value) fall back to the legacy per-event `normal` annotation.
 *
 * Feedback is immediate: suppression is a post-detection filter on the backend
 * (`_apply_allowlist`), so the same filter is applied *optimistically* to every
 * cached anomalies result here — the finding row disappears on click instead of
 * after a full detector re-scan (which on large cases took seconds to minutes
 * and read as "the button did nothing"). The write is confirmed with a toast;
 * failure rolls the rows back and surfaces the error via the global mutation
 * error toast. Detector re-runs pick the entry up server-side, so no blanket
 * `["anomalies"]` invalidation is needed (the sweep counts may lag until their
 * next refresh — they are a triage hint, not the authority).
 *
 * Shared by the field-value rows in EventDetailPanel (`detector: "*"`) and the
 * finding rows in the analysis views (detector-scoped) so both paths behave
 * identically and invalidate the same panels.
 */
export function useMarkNormal(caseId: string, timelineId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (t: MarkNormalTarget): Promise<void> => {
      if (t.field === undefined || t.value === undefined) {
        // Positional / value-less: keep the per-event annotation. Requires the
        // owning source + event to scope it — without them there is nothing to
        // mark, so surface that as an error rather than a false success.
        if (!t.sourceId || !t.eventId) {
          throw new Error("Cannot mark normal: no value key and no owning event to annotate.");
        }
        await annotationsApi.create(caseId, t.sourceId, t.eventId, "normal", "normal operation");
        return;
      }
      await baselinesApi.addAllowlist(caseId, timelineId, {
        detector: t.detector,
        field: t.field,
        value: t.value,
      });
    },
    onMutate: async (t) => {
      const prefix = ["anomalies", caseId, timelineId] as const;
      await qc.cancelQueries({ queryKey: prefix });
      const snapshots = qc.getQueriesData<AnomaliesResponse>({ queryKey: prefix });
      for (const [key, data] of snapshots) {
        if (!data) continue;
        // A detector-scoped entry only suppresses that detector's findings
        // (backend matches `entry.detector in (detector, "*")`); the query key
        // carries the detector at index 3.
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
      toast.success(
        t.field !== undefined && t.value !== undefined
          ? `Marked normal — ${t.field}=${t.value}`
          : "Marked event normal",
        "No longer flagged. Manage entries under Windows & normality.",
      );
      qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
      qc.invalidateQueries({ queryKey: ["allowlist", caseId, timelineId] });
    },
    meta: { errorTitle: "Mark normal failed" },
  });
}
