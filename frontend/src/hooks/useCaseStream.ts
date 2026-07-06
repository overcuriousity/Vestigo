import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { BASE } from "@/api/client";

/** Query key prefixes that reflect annotation/tag state and should be
 * refetched the moment any team member changes them. Kept in one place so
 * the invalidation logic mirrors the panels' actual query keys. Histogram,
 * anomaly-view, and viz keys are included because their queries honor tag
 * filters and render per-event tag state; `anomaly-fields` (cardinality
 * inventory), `semantic-search`/`similar` (user-input driven), and `events`
 * (merges annotation state from the `annotations` query) are deliberately
 * excluded. All these keys carry the caseId at index 1. */
export const INVALIDATE_PREFIXES = [
  "annotations",
  "tags",
  "tags-merged",
  "histogram",
  "anomalies-novelty",
  "anomalies-frequency",
  "field-histogram",
  "field-histogram-total",
  "field-terms",
  "viz-field-terms",
];

/** True when an annotation/tag change in *caseId* makes the query stale. */
export function shouldInvalidate(queryKey: readonly unknown[], caseId: string): boolean {
  return INVALIDATE_PREFIXES.includes(queryKey[0] as string) && queryKey[1] === caseId;
}

/**
 * Subscribes to the case's live-collaboration SSE stream and invalidates the
 * relevant TanStack Query caches whenever another team member changes
 * annotations/tags, so the event grid, tag chips, and tag autocomplete stay
 * in sync across analysts without a manual refresh or waiting for the
 * existing 30s poll (see `annotations` query in ExplorerPage).
 *
 * Advisory only: the SSE payload carries just IDs, never event content — the
 * actual data is always re-fetched through the normal authorized endpoints.
 */
export function useCaseStream(caseId: string | undefined) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!caseId) return;

    const source = new EventSource(`${BASE}/cases/${caseId}/stream`, {
      withCredentials: true,
    });

    source.onmessage = (event) => {
      if (!event.data) return; // keepalive comment lines don't reach onmessage
      try {
        const payload = JSON.parse(event.data) as { type?: string };
        if (payload.type === "annotation.changed") {
          queryClient.invalidateQueries({
            predicate: (query) => shouldInvalidate(query.queryKey, caseId),
          });
        }
      } catch {
        // Malformed event — ignore rather than crash the subscription.
      }
    };

    // EventSource auto-reconnects on transient errors by design; nothing to
    // do here beyond letting the browser retry (server sends `retry: 3000`).
    source.onerror = () => {};

    return () => source.close();
  }, [caseId, queryClient]);
}
