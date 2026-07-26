/**
 * The XHR transfer path exists only because `fetch` cannot report upload
 * progress, so these tests pin the things that path must not silently lose
 * relative to the `fetch` helpers: the session cookie, the multipart boundary,
 * the 401 handler, and FastAPI's error-detail shapes.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  ApiError,
  postFormWithProgress,
  getBlobWithProgress,
  setUnauthorizedHandler,
  type TransferProgress,
} from "@/api/client";

/** Minimal XMLHttpRequest stand-in: jsdom ships one, but it has no way to
 * drive upload progress or complete a request without a real server. */
class FakeXHR {
  static instances: FakeXHR[] = [];

  method = "";
  url = "";
  withCredentials = false;
  responseType = "";
  status = 0;
  statusText = "";
  responseText = "";
  response: unknown = null;
  sentBody: unknown = null;
  aborted = false;
  headers: Record<string, string> = {};

  private listeners: Record<string, ((e: unknown) => void)[]> = {};
  upload = {
    listeners: {} as Record<string, ((e: unknown) => void)[]>,
    addEventListener(type: string, fn: (e: unknown) => void) {
      (this.listeners[type] ??= []).push(fn);
    },
  };

  constructor() {
    FakeXHR.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }

  addEventListener(type: string, fn: (e: unknown) => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  send(body: unknown) {
    this.sentBody = body;
  }

  abort() {
    this.aborted = true;
    this.emit("abort", {});
  }

  emit(type: string, e: unknown) {
    for (const fn of this.listeners[type] ?? []) fn(e);
  }

  emitUpload(loaded: number, total: number, lengthComputable = true) {
    for (const fn of this.upload.listeners.progress ?? []) {
      fn({ loaded, total, lengthComputable });
    }
  }

  /** Complete the request with a status + body, as the browser would. */
  finish(status: number, body: string, statusText = "") {
    this.status = status;
    this.statusText = statusText;
    this.responseText = body;
    if (this.responseType === "blob") this.response = new Blob([body]);
    this.emit("load", {});
  }

  static last(): FakeXHR {
    const x = FakeXHR.instances.at(-1);
    if (!x) throw new Error("no XHR was created");
    return x;
  }
}

beforeEach(() => {
  FakeXHR.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("postFormWithProgress", () => {
  it("sends the session cookie and lets the browser set the multipart boundary", async () => {
    const form = new FormData();
    form.append("file", new Blob(["x"]), "a.vestigo");
    const p = postFormWithProgress<{ job_id: string }>("/cases/import", form);

    const xhr = FakeXHR.last();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toContain("/api/cases/import");
    // The httpOnly session cookie must be sent — this is `credentials: "include"`.
    expect(xhr.withCredentials).toBe(true);
    // A Content-Type here would clobber the multipart boundary.
    expect(xhr.headers["Content-Type"]).toBeUndefined();
    expect(xhr.sentBody).toBe(form);

    xhr.finish(200, JSON.stringify({ job_id: "j1" }));
    await expect(p).resolves.toEqual({ job_id: "j1" });
  });

  it("forwards upload progress, mapping a non-computable length to a null total", async () => {
    const seen: TransferProgress[] = [];
    const p = postFormWithProgress("/cases/import", new FormData(), {
      onProgress: (x) => seen.push(x),
    });

    const xhr = FakeXHR.last();
    xhr.emitUpload(5_000_000, 10_000_000);
    xhr.emitUpload(7_000_000, 0, false);
    xhr.finish(200, "{}");
    await p;

    expect(seen).toEqual([
      { loaded: 5_000_000, total: 10_000_000 },
      { loaded: 7_000_000, total: null },
    ]);
  });

  it("surfaces a string `detail` as an ApiError", async () => {
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().finish(400, JSON.stringify({ detail: "boom" }));
    await expect(p).rejects.toMatchObject({ name: "ApiError", status: 400, message: "boom" });
  });

  it("joins a Pydantic validation-error array like the fetch path does", async () => {
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().finish(
      422,
      JSON.stringify({
        detail: [
          { loc: ["body", "file"], msg: "field required" },
          { loc: ["body", "x"], msg: "bad" },
        ],
      }),
    );
    await expect(p).rejects.toMatchObject({
      message: "body.file: field required; body.x: bad",
    });
  });

  it("falls back to the status text for a non-JSON error body", async () => {
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().finish(502, "<html>bad gateway</html>", "Bad Gateway");
    await expect(p).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
  });

  it("rejects a malformed success body rather than resolving undefined", async () => {
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().finish(200, "not json");
    await expect(p).rejects.toMatchObject({ message: "Malformed response from server" });
  });

  it("invokes the unauthorized handler on 401", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().finish(401, JSON.stringify({ detail: "Not authenticated" }));
    await expect(p).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not invoke the unauthorized handler for a rejected login", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    const p = postFormWithProgress("/auth/login", new FormData());
    FakeXHR.last().finish(401, JSON.stringify({ detail: "Invalid credentials" }));
    await expect(p).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("reports a transport failure as status 0", async () => {
    const p = postFormWithProgress("/cases/import", new FormData());
    FakeXHR.last().emit("error", {});
    await expect(p).rejects.toMatchObject({ status: 0 });
  });

  it("aborts the request when the signal fires, with a distinguishable error", async () => {
    const controller = new AbortController();
    const p = postFormWithProgress("/cases/import", new FormData(), {
      signal: controller.signal,
    });
    const xhr = FakeXHR.last();
    controller.abort();
    expect(xhr.aborted).toBe(true);
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
  });

  it("rejects immediately for an already-aborted signal without opening a request", async () => {
    const controller = new AbortController();
    controller.abort();
    const p = postFormWithProgress("/cases/import", new FormData(), {
      signal: controller.signal,
    });
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
    expect(FakeXHR.instances).toHaveLength(0);
  });
});

describe("getBlobWithProgress", () => {
  it("resolves a Blob and reports download progress", async () => {
    const seen: TransferProgress[] = [];
    const p = getBlobWithProgress("/cases/c1/export/j1/download", {
      onProgress: (x) => seen.push(x),
    });
    const xhr = FakeXHR.last();
    expect(xhr.responseType).toBe("blob");
    expect(xhr.withCredentials).toBe(true);
    xhr.emit("progress", { loaded: 1024, total: 2048, lengthComputable: true });
    xhr.finish(200, "archive-bytes");
    const blob = await p;
    expect(blob).toBeInstanceOf(Blob);
    expect(seen).toEqual([{ loaded: 1024, total: 2048 }]);
  });

  it("reads the JSON error detail out of a blob-typed error response", async () => {
    const p = getBlobWithProgress("/cases/c1/export/j1/download");
    FakeXHR.last().finish(409, JSON.stringify({ detail: "Export not ready" }));
    await expect(p).rejects.toMatchObject({ status: 409, message: "Export not ready" });
  });
});
