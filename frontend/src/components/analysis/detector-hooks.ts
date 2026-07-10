/**
 * detector-hooks — non-component scaffolding shared by every statistical-
 * detector view: baseline-frame resolution, auto-scan field selection,
 * findings capping, marker/runId plumbing. Split from detector-shared.tsx so
 * that file only exports components (react fast-refresh requirement); the
 * shared row/toolbar chrome components stay there.
 */
import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { eventsApi } from "@/api/events";
import { useBaselineStore } from "@/stores/baseline";
import type { AnomalyMarker, Event } from "@/api/types";

/**
 * Resolve the request params + queryKey fragment for the current global
 * detector frame, read from the baseline store (not a per-view arg). Every
 * detector view calls this so a change to the frame or active baseline re-runs
 * the scan and all views stay consistent. `needsBaseline` is true when the
 * frame is `baseline` but no definition is active — the view should prompt to
 * pick/build one rather than silently fall back to the legacy midpoint split.
 */
export function useBaselineRequest(): {
  params: { temporal?: boolean; baseline_id?: string };
  key: string;
  needsBaseline: boolean;
} {
  const frame = useBaselineStore((s) => s.frame);
  const activeBaselineId = useBaselineStore((s) => s.activeBaselineId);
  if (frame !== "baseline") return { params: { temporal: false }, key: "self", needsBaseline: false };
  if (activeBaselineId)
    return { params: { baseline_id: activeBaselineId }, key: `bl:${activeBaselineId}`, needsBaseline: false };
  return { params: { temporal: false }, key: "self", needsBaseline: true };
}

// Auto-scan field selection for the string detectors (charset/entropy). Mirrors
// _select_auto_scan_tokens / _MAX_AUTO_SCAN_FIELDS / _AUTO_IDENTIFIER_RESERVE in
// db/anomaly_stats.py so the picker's "auto" preview matches what the backend
// actually scans (categorical + identifier fields, with reserved identifier
// slots) — the two must stay in sync.
export const AUTO_SCAN_MAX_FIELDS = 15;
const AUTO_IDENTIFIER_RESERVE = 5;

/**
 * Blend categorical and identifier field tokens under the auto-scan cap, each
 * list already best-first. Identifier fields get up to AUTO_IDENTIFIER_RESERVE
 * reserved slots so a wide categorical set can't crowd them out; each kind
 * backfills the other's unused slots.
 */
export function selectAutoScanTokens(cats: string[], ids: string[]): string[] {
  const reserve = Math.min(ids.length, AUTO_IDENTIFIER_RESERVE);
  const picked = cats.slice(0, AUTO_SCAN_MAX_FIELDS - reserve);
  picked.push(...ids.slice(0, AUTO_SCAN_MAX_FIELDS - picked.length));
  if (picked.length < AUTO_SCAN_MAX_FIELDS) {
    for (const t of cats) {
      if (picked.length >= AUTO_SCAN_MAX_FIELDS) break;
      if (!picked.includes(t)) picked.push(t);
    }
  }
  return picked;
}

/**
 * Encode a field selection for the anomalies API: null → auto (omit the param),
 * a non-empty set → comma-joined tokens, an empty set → the "__none__" sentinel
 * the backend recognises as "explicitly scan nothing". Returns undefined for
 * the auto case so callers can spread it conditionally.
 */
export function fieldsParamOf(selectedFields: string[] | null): string | undefined {
  if (selectedFields === null) return undefined;
  return selectedFields.length > 0 ? selectedFields.join(",") : "__none__";
}

/**
 * Cap a ranked findings list to the top `initial` (the backend already returns
 * them severity-first), with a toggle to reveal the rest. Keeps a long scan
 * from rendering as an undifferentiated wall — the worst are on top, the tail
 * is one click away.
 */
export function useCappedFindings<T>(findings: T[], initial = 20) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? findings : findings.slice(0, initial);
  return {
    shown,
    total: findings.length,
    hasMore: findings.length > initial,
    expanded,
    toggle: () => setExpanded((v) => !v),
  };
}

/**
 * Server-side findings limit with stepped "load more" (…→50→150→500, capped by
 * the API's `le=500`). Distinct from `useCappedFindings`, which only trims the
 * client-side render of what the server already returned — this raises how
 * much the server computes and returns. Include `limit` in the query key so
 * raising it refetches.
 */
export const FINDINGS_LIMIT_STEPS = [50, 150, 500];

export function useFindingsLimit(initial = 50) {
  const [limit, setLimit] = useState(initial);
  const next = FINDINGS_LIMIT_STEPS.find((s) => s > limit);
  return {
    limit,
    canRaise: next !== undefined,
    raise: () => {
      if (next !== undefined) setLimit(next);
    },
  };
}

/**
 * Publish the active view's findings as histogram/grid markers, clearing
 * them on unmount or when the finding set changes. `build` may return null
 * to skip findings without a usable timestamp.
 */
export function useAnomalyMarkers<T>(
  findings: T[],
  build: (finding: T) => AnomalyMarker | null,
  onFindingsChange?: (markers: AnomalyMarker[]) => void,
) {
  useEffect(() => {
    if (!onFindingsChange) return;
    const markers = findings
      .map(build)
      .filter((m): m is AnomalyMarker => m !== null);
    onFindingsChange(markers);
    return () => onFindingsChange([]);
    // `build` closes over per-render display data derived from the same
    // query result as `findings` (stable react-query reference) — keying the
    // effect on `findings` alone matches the pre-extraction behavior and
    // avoids a re-fire loop on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findings]);
}

/** Mutation that fetches a finding's full event by id and surfaces it. */
export function useOpenEvent(
  caseId: string,
  timelineId: string,
  eventId: string | null | undefined,
  onSelectEvent: (event: Event) => void,
) {
  return useMutation({
    mutationFn: () => eventsApi.getById(caseId, timelineId, eventId!),
    onSuccess: (event) => {
      if (event) onSelectEvent(event);
    },
    meta: { errorTitle: "Couldn't open event" },
  });
}

/** Publish the active view's persisted run_id, clearing it on unmount. */
export function useDetectorRunId(
  runId: string | null | undefined,
  onRunIdChange?: (runId: string | undefined) => void,
) {
  useEffect(() => {
    if (!onRunIdChange) return;
    onRunIdChange(runId ?? undefined);
    return () => onRunIdChange(undefined);
  }, [runId, onRunIdChange]);
}
