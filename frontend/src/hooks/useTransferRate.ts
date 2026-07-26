/**
 * Client-side throughput/ETA for a browser-side byte transfer (case archive
 * upload and download).
 *
 * Ingest jobs get their rate from the backend's Kalman filter (`core/eta.py`),
 * but an upload never reaches a job until it has fully landed, and a download
 * is streamed straight to the browser — neither has a server-side estimator.
 * The numbers are emitted in the same `rate_bps`/`eta_s` shape `JobStatusRow`
 * already renders, so the whole "MB/s · ~Xs left" line comes for free.
 *
 * Deliberately a plain average over the whole transfer rather than a windowed
 * rate: an archive transfer is one long sequential stream, so the average is
 * both steadier and more honest than a smoothed instantaneous estimate.
 */
import { useCallback, useRef, useState } from "react";
import type { TransferProgress } from "@/api/client";

export interface TransferState {
  loaded: number;
  total: number | null;
  rate_bps: number | null;
  eta_s: number | null;
}

export interface UseTransferRate {
  /** Null until the first progress event arrives. */
  state: TransferState | null;
  /** Feed a progress event; safe to pass directly as `onProgress`. */
  report: (p: TransferProgress) => void;
  /** Clear state and restart the clock (call before each attempt). */
  reset: () => void;
}

export function useTransferRate(): UseTransferRate {
  const [state, setState] = useState<TransferState | null>(null);
  const startedAtRef = useRef<number | null>(null);

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

  const reset = useCallback(() => {
    startedAtRef.current = null;
    setState(null);
  }, []);

  return { state, report, reset };
}
