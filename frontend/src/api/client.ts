/**
 * Typed API client for Vestigo.
 *
 * Handles:
 * - Base URL from env (defaults to same-origin for nginx deployment)
 * - JSON fetch with envelope normalization
 * - Streaming download (export)
 * - Typed error surface
 */

export const BASE = (import.meta.env.VITE_API_BASE ?? "") + "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Extract a human-readable message from a FastAPI error body's `detail`,
 * which may be a plain string or a Pydantic validation error array. */
function extractErrorDetail(json: unknown, fallback: string): string {
  const detail = (json as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        if (e && typeof e === "object" && "msg" in e) {
          const loc = Array.isArray((e as { loc?: unknown }).loc)
            ? (e as { loc: unknown[] }).loc.join(".")
            : undefined;
          const msg = String((e as { msg: unknown }).msg);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(e);
      })
      .join("; ");
  }
  return fallback;
}

/**
 * Called whenever a request comes back 401 (no/expired/revoked session).
 * Wired up by `App.tsx` via `setUnauthorizedHandler(...)` to clear the
 * cached user and redirect to `/login`, without creating an import cycle
 * between the API layer and the auth store.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/**
 * Shared 401-handling + error-surfacing, body-agnostic so both the `fetch`
 * helpers below and the `XMLHttpRequest` transfer path can use it. Excludes
 * `/auth/login` from `onUnauthorized` — a 401 there is just a rejected login
 * attempt, not a session that needs to redirect to itself.
 *
 * Returns rather than throws: XHR callbacks must `reject(err)`, and there is
 * no `throw` position in an event listener that a promise would observe.
 */
export function apiErrorFromBody(
  status: number,
  statusText: string,
  bodyText: string,
  path: string,
): ApiError {
  if (status === 401 && path !== "/auth/login") {
    onUnauthorized?.();
  }
  let detail = statusText;
  try {
    detail = extractErrorDetail(JSON.parse(bodyText), detail);
  } catch {
    // Non-JSON body (proxy error page, empty 502): keep the status text.
  }
  return new ApiError(status, detail);
}

/** Shared 401-handling + error-surfacing for every fetch helper below. */
async function checkResponse(res: Response, path: string): Promise<void> {
  if (!res.ok) {
    // A 401 always implies `!res.ok`, so the `onUnauthorized` call inside
    // `apiErrorFromBody` still fires for every unauthorized response.
    throw apiErrorFromBody(res.status, res.statusText, await res.text(), path);
  }
}

/** Query-string values a request may carry; arrays repeat the key. */
export type QueryParams = Record<
  string,
  string | number | boolean | string[] | undefined | null
>;

async function request<T>(
  method: string,
  path: string,
  opts?: {
    body?: unknown;
    params?: QueryParams;
    signal?: AbortSignal;
  },
): Promise<T> {
  const url = new URL(BASE + path, window.location.href);
  if (opts?.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      // An array becomes a repeated param (`?fields=a&fields=b`), which is
      // how FastAPI reads a `list[str]` query parameter.
      if (Array.isArray(v)) {
        for (const item of v) url.searchParams.append(k, String(item));
      } else if (v != null && v !== "") {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const headers: Record<string, string> = {};
  let reqBody: BodyInit | undefined;
  if (opts?.body !== undefined) {
    headers["Content-Type"] = "application/json";
    reqBody = JSON.stringify(opts.body);
  }

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: reqBody,
    signal: opts?.signal,
    // Sessions are an httpOnly cookie — this is required for it to be sent
    // (and accepted) both same-origin (vestigo-web on :8080) and cross-origin
    // during dev (Vite on :5173 proxying to :8080).
    credentials: "include",
  });

  await checkResponse(res, path);

  return res.json() as Promise<T>;
}

// Convenience verbs
export const get = <T>(
  path: string,
  params?: QueryParams,
  signal?: AbortSignal,
) => request<T>("GET", path, { params, signal });

export const post = <T>(path: string, body?: unknown) =>
  request<T>("POST", path, { body });

export const patch = <T>(path: string, body?: unknown) =>
  request<T>("PATCH", path, { body });

export const put = <T>(path: string, body?: unknown) =>
  request<T>("PUT", path, { body });

export const del = <T>(path: string, params?: Record<string, string | number | boolean | undefined | null>) =>
  request<T>("DELETE", path, { params });

/** POST with multipart form data (for file upload). */
export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const url = BASE + path;
  const res = await fetch(url, { method: "POST", body: form, credentials: "include" });
  await checkResponse(res, path);
  return res.json() as Promise<T>;
}

/** Trigger a streaming download (JSON POST body). Returns a Blob. */
export async function fetchBlob(path: string, body: unknown): Promise<Blob> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  await checkResponse(res, path);
  return res.blob();
}

/** GET a resource as a Blob (e.g. a CSV/JSONL download via query params). */
export async function fetchBlobGet(
  path: string,
  params?: Record<string, string | number | boolean | undefined | null>,
): Promise<Blob> {
  const url = new URL(BASE + path, window.location.href);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const res = await fetch(url.toString(), { credentials: "include" });
  await checkResponse(res, path);
  return res.blob();
}

// ---------------------------------------------------------------------------
// Progress-reporting transfer path (XMLHttpRequest)
//
// `fetch` cannot report *upload* progress — there is no event for bytes sent,
// and a ReadableStream request body is not supported without HTTP/2 duplex.
// Case import/export archives are multi-GB, so the two transfer calls go
// through XHR instead. Everything that matters (401 handling, error detail
// parsing, ApiError shape) is shared via `apiErrorFromBody`; only the ~6 lines
// of request wiring are duplicated. The `fetch` helpers above are deliberately
// left alone — they are the common path and are covered by existing tests.
// ---------------------------------------------------------------------------

/** Bytes moved so far. `total` is null when the length is not computable
 * (chunked response, or an upload the browser can't size). */
export interface TransferProgress {
  loaded: number;
  total: number | null;
}

export type ProgressHandler = (p: TransferProgress) => void;

interface XhrOptions {
  method: string;
  /** API path (no BASE prefix) — used for the `/auth/login` 401 exemption. */
  path: string;
  url: string;
  /** Narrower than `fetch`'s BodyInit: XHR cannot send a ReadableStream. */
  body?: XMLHttpRequestBodyInit | null;
  responseType?: "" | "blob";
  onUploadProgress?: ProgressHandler;
  onDownloadProgress?: ProgressHandler;
  signal?: AbortSignal;
}

function progressOf(e: ProgressEvent): TransferProgress {
  return { loaded: e.loaded, total: e.lengthComputable ? e.total : null };
}

function xhrRequest<T>(opts: XhrOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (opts.signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open(opts.method, opts.url);
    // The `credentials: "include"` equivalent: sessions are an httpOnly cookie,
    // needed both same-origin (:8080) and via the Vite dev proxy (:5173).
    xhr.withCredentials = true;
    if (opts.responseType) xhr.responseType = opts.responseType;
    // No Content-Type is ever set: a FormData body needs the browser to write
    // the multipart boundary itself.

    if (opts.onUploadProgress) {
      const onUpload = opts.onUploadProgress;
      xhr.upload.addEventListener("progress", (e) => onUpload(progressOf(e)));
    }
    if (opts.onDownloadProgress) {
      const onDownload = opts.onDownloadProgress;
      xhr.addEventListener("progress", (e) => onDownload(progressOf(e)));
    }

    const onAbort = () => xhr.abort();
    opts.signal?.addEventListener("abort", onAbort);
    const cleanup = () => opts.signal?.removeEventListener("abort", onAbort);

    xhr.addEventListener("load", () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) {
        if (opts.responseType === "blob") {
          resolve(xhr.response as T);
          return;
        }
        try {
          resolve(JSON.parse(xhr.responseText) as T);
        } catch {
          reject(new ApiError(xhr.status, "Malformed response from server"));
        }
        return;
      }
      // Error bodies are JSON even when the success path is a blob.
      const rejectWith = (text: string) =>
        reject(apiErrorFromBody(xhr.status, xhr.statusText, text, opts.path));
      if (opts.responseType === "blob" && xhr.response instanceof Blob) {
        xhr.response.text().then(rejectWith, () => rejectWith(""));
      } else {
        rejectWith(xhr.responseText ?? "");
      }
    });

    const fail = () => {
      cleanup();
      // status 0 is the "never reached the server" sentinel the transfer
      // dialogs use to word their retry copy.
      reject(new ApiError(0, "Network error — the transfer was interrupted"));
    };
    xhr.addEventListener("error", fail);
    xhr.addEventListener("timeout", fail);
    xhr.addEventListener("abort", () => {
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    });

    xhr.send(opts.body ?? null);
  });
}

/** POST multipart form data, reporting upload progress. Abortable — callers
 * that abort before the server responds leave nothing behind. */
export function postFormWithProgress<T>(
  path: string,
  form: FormData,
  opts?: { onProgress?: ProgressHandler; signal?: AbortSignal },
): Promise<T> {
  return xhrRequest<T>({
    method: "POST",
    path,
    url: BASE + path,
    body: form,
    onUploadProgress: opts?.onProgress,
    signal: opts?.signal,
  });
}

/** GET a resource as a Blob, reporting download progress. */
export function getBlobWithProgress(
  path: string,
  opts?: { onProgress?: ProgressHandler; signal?: AbortSignal },
): Promise<Blob> {
  return xhrRequest<Blob>({
    method: "GET",
    path,
    url: new URL(BASE + path, window.location.href).toString(),
    responseType: "blob",
    onDownloadProgress: opts?.onProgress,
    signal: opts?.signal,
  });
}
