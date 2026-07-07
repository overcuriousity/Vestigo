import { useQuery } from "@tanstack/react-query";
import { get } from "./client";
import type { HealthResponse } from "./types";

export const healthApi = {
  check: () => get<HealthResponse>("/health"),
};

/**
 * Shared `["health"]` query used by the top bar, login page, and embed wizard.
 * One hook so the polling cadence and staleness are consistent across every
 * consumer of the (single, deduped) health query instead of each passing its
 * own options to the same key. Polls every 15s so capability gates (e.g. the
 * embed wizard's embeddings check) recover after a transient health failure.
 */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: healthApi.check,
    staleTime: 30_000,
    refetchInterval: 15_000,
  });
}
