/**
 * One hook for every file transfer the app performs — source upload, case
 * import, case export download, event export, enricher asset.
 *
 * It exists because each of those needs the same four things, and getting any
 * one of them wrong is a real bug rather than a rough edge:
 *
 * 1. **A synchronous submit guard.** `disabled={isPending}` is not one: a
 *    second click can land in the same task as the first, before React has
 *    re-rendered the button. On a multi-GB upload that window stays open for
 *    minutes — this is exactly how a case archive got imported twice (#184).
 *    The ref, not the state, is what actually closes it.
 * 2. **An abort signal.** Cancelling is safe at every upload site by
 *    construction: the server streams the body to a temp file
 *    (`api/uploads.py::receive_upload_to_tmp`) and creates rows and jobs only
 *    once the whole thing has landed, so an aborted upload leaves nothing to
 *    clean up. A cancel is a user decision, not a failure, and never surfaces
 *    as an error.
 * 3. **Throughput and ETA.** Ingest jobs get a rate from the backend's Kalman
 *    filter (`core/eta.py`), but an upload has no job until it has landed and
 *    a download never gets one, so the browser has to derive its own. A plain
 *    average over the whole transfer, deliberately: one long sequential stream
 *    is steadier and more honest under an average than under a smoothed
 *    instantaneous estimate.
 * 4. **One reading of a failed transfer.** `ApiError` with status 0 is the XHR
 *    "never reached the server" sentinel, which needs different copy from a
 *    server-side rejection.
 *
 * TanStack Query still owns the request — this wraps `useMutation` rather than
 * replacing it, so uploads do not become the one subsystem with its own
 * request lifecycle.
 */
import { useCallback, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, type TransferOptions, type TransferProgress } from "@/api/client";

export interface TransferState {
  loaded: number;
  /** Null when the length is not computable — a chunked response with no
   * `Content-Length`. The UI renders that as an indeterminate bar. */
  total: number | null;
  rate_bps: number | null;
  eta_s: number | null;
}

/** True for the error an aborted transfer rejects with. */
export function isAbortError(e: unknown): boolean {
  return (e as Error | undefined)?.name === "AbortError";
}

/** Human copy for a failed transfer, distinguishing "the server said no" from
 * "the bytes stopped moving". */
export function transferErrorMessage(e: unknown): string {
  if (e instanceof ApiError && e.status === 0) {
    return "The transfer was interrupted before it finished.";
  }
  return (e as Error).message;
}

export interface UseFileTransferOptions<TData, TVars = void> {
  /** Runs the request. Pass the given options straight through to the API
   * helper so progress and cancellation actually reach the wire. `vars` is
   * whatever `submit` was called with — for a picker that uploads the moment
   * a file is chosen, that is the file itself, which has not reached state
   * yet at the point the transfer starts. */
  mutationFn: (opts: TransferOptions, vars: TVars) => Promise<TData>;
  onSuccess?: (data: TData, vars: TVars) => void;
  /** Receives the already-worded message; the hook also stores it in `error`. */
  onError?: (message: string, cause: unknown) => void;
  /** Called instead of `onError` when the user aborted. */
  onCancel?: () => void;
}

export interface FileTransfer<TData, TVars = void> {
  /** Start the transfer. A no-op while one is already in flight — the guard is
   * synchronous, so a double-click cannot slip past it. */
  submit: (vars: TVars) => void;
  /** Abort an in-flight transfer. Resolves through `onCancel`, not `onError`. */
  cancel: () => void;
  /** Clear progress, error and result — call when reopening a dialog. */
  reset: () => void;
  /** In flight. Drives `disabled` and button labels. */
  active: boolean;
  /** Null until the first progress event arrives. */
  state: TransferState | null;
  error: string | null;
  data: TData | undefined;
}

export function useFileTransfer<TData, TVars = void>(
  opts: UseFileTransferOptions<TData, TVars>,
): FileTransfer<TData, TVars> {
  const { mutationFn, onSuccess, onError, onCancel } = opts;

  const [state, setState] = useState<TransferState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(false);
  const startedAtRef = useRef<number | null>(null);
  const submittingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  // Kept in refs so `submit` stays stable and a re-render mid-transfer can
  // never call a stale handler.
  const handlers = useRef({ mutationFn, onSuccess, onError, onCancel });
  handlers.current = { mutationFn, onSuccess, onError, onCancel };

  const report = useCallback((p: TransferProgress) => {
    const now = Date.now();
    if (startedAtRef.current == null) startedAtRef.current = now;
    const elapsedS = (now - startedAtRef.current) / 1000;
    // Below ~250ms the elapsed time is dominated by scheduling jitter and the
    // derived rate is nonsense, so withhold it rather than show a wild number.
    const rate = elapsedS > 0.25 && p.loaded > 0 ? p.loaded / elapsedS : null;
    const remaining = p.total != null ? Math.max(0, p.total - p.loaded) : null;
    setState({
      loaded: p.loaded,
      total: p.total,
      rate_bps: rate,
      eta_s: rate != null && rate > 0 && remaining != null ? remaining / rate : null,
    });
  }, []);

  const mutation = useMutation({
    mutationFn: (vars: TVars) => {
      const controller = new AbortController();
      abortRef.current = controller;
      return handlers.current.mutationFn(
        { onProgress: report, signal: controller.signal },
        vars,
      );
    },
    onSuccess: (data, vars) => handlers.current.onSuccess?.(data, vars),
    onError: (e) => {
      if (isAbortError(e)) {
        setState(null);
        handlers.current.onCancel?.();
        return;
      }
      const message = transferErrorMessage(e);
      setError(message);
      handlers.current.onError?.(message, e);
    },
    onSettled: () => {
      submittingRef.current = false;
      abortRef.current = null;
      setActive(false);
    },
    // Failures render inside the dialog that owns the transfer; a global toast
    // on top of that would double-report every cancelled upload.
    meta: { silentError: true },
  });

  const { mutate, reset: resetMutation } = mutation;

  const submit = useCallback(
    (vars: TVars) => {
      if (submittingRef.current) return;
      submittingRef.current = true;
      startedAtRef.current = null;
      setActive(true);
      setState(null);
      setError(null);
      mutate(vars);
    },
    [mutate],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const reset = useCallback(() => {
    startedAtRef.current = null;
    setState(null);
    setError(null);
    resetMutation();
  }, [resetMutation]);

  return { submit, cancel, reset, active, state, error, data: mutation.data };
}
