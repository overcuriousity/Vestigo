/**
 * A 503 from a full scan lane (#300) is "still waiting", not "failed": the
 * query keeps retrying at the server's pace and the UI names the queue.
 */
import { describe, it, expect } from "vitest";
import { ApiError } from "@/api/client";
import { busyMessage, busyRetry, isScanBusy } from "@/lib/queryClient";

function busy(ahead: number): ApiError {
  const e = new ApiError(503, "scan lane busy");
  e.queuedAhead = ahead;
  e.retryAfterMs = 5000;
  return e;
}

describe("busyRetry", () => {
  it("keeps retrying a busy lane and stops on anything else", () => {
    expect(isScanBusy(busy(2))).toBe(true);
    expect(isScanBusy(new ApiError(503, "down"))).toBe(false);
    expect(busyRetry.retry(7, busy(2))).toBe(true);
    expect(busyRetry.retry(0, new ApiError(500, "x"))).toBe(false); // surfaces at once
  });

  it("honours Retry-After and names the queue", () => {
    expect(busyRetry.retryDelay(3, busy(2))).toBe(5000);
    expect(busyMessage(busy(2))).toBe("Waiting behind 2 scans…");
    expect(busyMessage(busy(1))).toBe("Waiting behind 1 scan…");
    expect(busyMessage(busy(0))).toBe("Waiting for a scan slot…");
    expect(busyMessage(new Error("x"))).toBeNull();
  });
});
