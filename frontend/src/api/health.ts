import { useQuery } from "@tanstack/react-query";
import { get } from "./client";
import type { Capabilities, HealthResponse } from "./types";

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

/** Optimistic default: assume a subsystem is there until health says otherwise.
 * The alternative — hiding everything while the first health request is in
 * flight — makes the app flicker on every load, and every gated endpoint
 * refuses on its own anyway (a hidden button is never the only enforcement). */
const ASSUME_AVAILABLE: Capabilities = {
  embeddings: true,
  agent: false, // except the agent: it has always stayed hidden until probed.
  mcp: false,
  oidc: false,
  enrichers: true,
  sigma: true,
  transfer: true,
  // Like the agent: stays hidden until probed. Offering to load a demo case
  // is only honest once health confirms the instance will actually seed one.
  demo_case: false,
};

/**
 * Which optional subsystems this installation has configured.
 *
 * An unconfigured subsystem renders no entry point at all — no disabled button
 * to explain, no error the analyst has to interpret. Gate on this rather than
 * on the individual legacy flags so every subsystem behaves the same way.
 */
export function useCapabilities(): Capabilities {
  return useHealth().data?.capabilities ?? ASSUME_AVAILABLE;
}
