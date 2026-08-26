import { describe, it, expect } from "vitest";
import { ApiError, apiErrorFromBody } from "@/api/client";

describe("ApiError", () => {
  it("has the right name, message and status", () => {
    const err = new ApiError(404, "Not Found");
    expect(err.name).toBe("ApiError");
    expect(err.message).toBe("Not Found");
    expect(err.status).toBe(404);
    expect(err instanceof Error).toBe(true);
    expect(err instanceof ApiError).toBe(true);
  });
});

describe("apiErrorFromBody", () => {
  it("carries queued_ahead and Retry-After from a busy 503", () => {
    const err = apiErrorFromBody(
      503,
      "Service Unavailable",
      JSON.stringify({ detail: "scan lane busy: 3 waiting ahead", queued_ahead: 3 }),
      "/x",
      { "retry-after": "5" },
    );
    expect(err.status).toBe(503);
    expect(err.message).toBe("scan lane busy: 3 waiting ahead");
    expect(err.queuedAhead).toBe(3);
    expect(err.retryAfterMs).toBe(5000);
  });

  it("leaves the busy fields unset on an ordinary error", () => {
    const err = apiErrorFromBody(500, "Internal", JSON.stringify({ detail: "boom" }), "/x");
    expect(err.queuedAhead).toBeUndefined();
    expect(err.retryAfterMs).toBeUndefined();
  });
});
