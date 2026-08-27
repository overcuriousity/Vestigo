/**
 * The app's single QueryClient, with global success/error feedback wired at
 * the cache level so every mutation in the app surfaces failures without
 * per-call-site boilerplate (before this, 2 of ~80 mutations handled errors —
 * a failed click was silent).
 *
 * Per-mutation opt-in/out via `meta`:
 *   - `meta.successToast: string`  — show a success toast with this title.
 *   - `meta.errorTitle: string`    — toast title on failure (default "Action failed").
 *   - `meta.silentError: true`     — suppress the global error toast (for
 *     forms that render the error inline next to the button, e.g. login).
 * Queries support `meta.silentError` the same way.
 */
import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { toast } from "@/stores/toasts";

declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: {
      successToast?: string;
      errorTitle?: string;
      silentError?: boolean;
    };
    queryMeta: {
      silentError?: boolean;
    };
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 401s are handled globally (session cleared → redirect to login) — a toast
 * on top of the redirect is noise. */
function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** A 503 from a full scan lane (#300): the server is saying "wait", not "no". */
export function isScanBusy(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 503 && error.queuedAhead !== undefined;
}

/**
 * How many times a busy lane is re-asked before the 503 is allowed to surface.
 * At the server's 5s `Retry-After` that is about two minutes — long enough to
 * outlast a detector sweep, short enough that a genuinely wedged lane ends in
 * an error an analyst can act on. Retrying forever is the same silent stall
 * #300 set out to remove: a busy lane raises no toast, and a panel still
 * holding previous data never even reaches the spinner that names the queue.
 */
export const BUSY_RETRY_LIMIT = 24;

/**
 * Query options for surfaces that read a scan lane: a busy lane is "still
 * waiting", never "failed", so keep asking at the server's pace — but only
 * up to `BUSY_RETRY_LIMIT` times, after which it *is* a failure and says so.
 * Any other failure surfaces at once — these are charts the analyst is
 * looking at, and a second of silent retry before the error is a second of
 * spinner for nothing.
 */
export const busyRetry = {
  retry: (count: number, error: unknown): boolean => isScanBusy(error) && count < BUSY_RETRY_LIMIT,
  retryDelay: (_count: number, error: unknown): number =>
    isScanBusy(error) ? (error.retryAfterMs ?? 5000) : 0,
};

/** What to show in place of a spinner while a busy lane is being retried. */
export function busyMessage(error: unknown): string | null {
  if (!isScanBusy(error)) return null;
  const n = error.queuedAhead ?? 0;
  return n > 0 ? `Waiting behind ${n} scan${n === 1 ? "" : "s"}…` : "Waiting for a scan slot…";
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
  mutationCache: new MutationCache({
    onSuccess: (_data, _vars, _ctx, mutation) => {
      const title = mutation.meta?.successToast;
      if (title) toast.success(title);
    },
    onError: (error, _vars, _ctx, mutation) => {
      if (mutation.meta?.silentError || isUnauthorized(error)) return;
      toast.error(mutation.meta?.errorTitle ?? "Action failed", errorMessage(error));
    },
  }),
  queryCache: new QueryCache({
    onError: (error, query) => {
      // Reached only once retries are exhausted, so a busy scan lane landing
      // here has stayed busy for the whole `BUSY_RETRY_LIMIT` window (#300):
      // a failure the analyst should hear about, not a wait to keep hiding.
      // While it is still being retried the query never enters the error
      // state and this never runs.
      if (query.meta?.silentError || isUnauthorized(error)) return;
      // Background refetch failures of data already on screen are surfaced
      // too — a stale panel silently pretending to be current is worse than
      // a toast. The store dedups identical messages, so one dead endpoint
      // feeding several panels produces a single toast.
      toast.error("Loading failed", errorMessage(error));
    },
  }),
});
