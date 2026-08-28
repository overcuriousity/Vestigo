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
  /** Set on a 503 from a full scan lane: callers parked ahead of this request. */
  queuedAhead?: number;
  /** `Retry-After` in milliseconds, when the server sent one. */
  retryAfterMs?: number;
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
  headers?: Record<string, string> | Headers,
): ApiError {
  if (status === 401 && path !== "/auth/login") {
    onUnauthorized?.();
  }
  let detail = statusText;
  let queuedAhead: number | undefined;
  try {
    const json: unknown = JSON.parse(bodyText);
    detail = extractErrorDetail(json, detail);
    // A full scan lane (#300) answers 503 with the queue depth beside `detail`;
    // that is what lets the UI say "waiting" instead of "failed".
    const ahead = (json as { queued_ahead?: unknown } | null)?.queued_ahead;
    if (typeof ahead === "number") queuedAhead = ahead;
  } catch {
    // Non-JSON body (proxy error page, empty 502): keep the status text.
  }
  const err = new ApiError(status, detail);
  if (queuedAhead !== undefined) err.queuedAhead = queuedAhead;
  const retryAfter =
    headers instanceof Headers ? headers.get("retry-after") : headers?.["retry-after"];
  if (retryAfter && /^\d+$/.test(retryAfter)) err.retryAfterMs = Number(retryAfter) * 1000;
  return err;
}

/** Shared 401-handling + error-surfacing for every fetch helper below. */
async function checkResponse(res: Response, path: string): Promise<void> {
  if (!res.ok) {
    // A 401 always implies `!res.ok`, so the `onUnauthorized` call inside
    // `apiErrorFromBody` still fires for every unauthorized response.
    throw apiErrorFromBody(res.status, res.statusText, await res.text(), path, res.headers);
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

// ---------------------------------------------------------------------------
// File-transfer path (XMLHttpRequest)
//
// `fetch` cannot report *upload* progress — there is no event for bytes sent,
// and a ReadableStream request body is not supported without HTTP/2 duplex.
// Every file-bearing call therefore goes through XHR: uploads (multi-GB log
// sources, case archives, enricher assets) and blob downloads (case archives,
// event exports) alike. There is deliberately no second, progress-less way to
// send a file — `postForm`/`fetchBlob`/`fetchBlobGet` below *are* this path,
// so a new call site cannot opt out of progress by picking the wrong helper.
//
// Plain JSON verbs (`get`/`post`/`patch`/`put`/`del`) stay on `fetch` above:
// they carry no file body, so there is nothing to report, and `fetch` is the
// better-supported primitive for them. Both cores share `apiErrorFromBody`,
// so there is one error surface regardless of which one ran the request.
// ---------------------------------------------------------------------------

/** Bytes moved so far. `total` is null when the length is not computable
 * (chunked response, or an upload the browser can't size). */
export interface TransferProgress {
  loaded: number;
  total: number | null;
}

export type ProgressHandler = (p: TransferProgress) => void;

/** Byte-progress + cancellation, accepted by every file-bearing helper. */
export interface TransferOptions {
  onProgress?: ProgressHandler;
  signal?: AbortSignal;
}

interface XhrOptions {
  method: string;
  /** API path (no BASE prefix) — used for the `/auth/login` 401 exemption. */
  path: string;
  url: string;
  /** Narrower than `fetch`'s BodyInit: XHR cannot send a ReadableStream. */
  body?: XMLHttpRequestBodyInit | null;
  headers?: Record<string, string>;
  responseType?: "" | "blob";
  onUploadProgress?: ProgressHandler;
  onDownloadProgress?: ProgressHandler;
  signal?: AbortSignal;
}

/** Absolute URL for an API path, with `request()`'s query-param semantics
 * (arrays repeat the key, empty/nullish values are dropped). */
function apiUrl(path: string, params?: QueryParams): string {
  const url = new URL(BASE + path, window.location.href);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) {
      for (const item of v) url.searchParams.append(k, String(item));
    } else if (v != null && v !== "") {
      url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
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
    // Headers are opt-in per caller, never defaulted: a FormData body must go
    // out with no Content-Type at all so the browser writes the multipart
    // boundary itself.
    for (const [k, v] of Object.entries(opts.headers ?? {})) {
      xhr.setRequestHeader(k, v);
    }

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
        reject(
          apiErrorFromBody(xhr.status, xhr.statusText, text, opts.path, {
            "retry-after": xhr.getResponseHeader("retry-after") ?? "",
          }),
        );
      if (opts.responseType === "blob") {
        // `responseText` *throws* InvalidStateError unless responseType is ""
        // or "text", and this is an event listener — the throw would escape
        // into nothing and leave the promise forever pending. So a blob
        // request only ever reads its body through `response`.
        if (xhr.response instanceof Blob) {
          xhr.response.text().then(rejectWith, () => rejectWith(""));
        } else {
          rejectWith("");
        }
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

/**
 * POST multipart form data. `onProgress` reports bytes *sent*; `signal`
 * aborts the upload.
 *
 * Aborting is safe at every current call site by construction: the server
 * streams the body to a temp file (`api/uploads.py::receive_upload_to_tmp`)
 * and only creates rows and jobs once the whole thing has landed, so an
 * upload cut short leaves nothing behind.
 */
export function postForm<T>(path: string, form: FormData, opts?: TransferOptions): Promise<T> {
  return xhrRequest<T>({
    method: "POST",
    path,
    url: apiUrl(path),
    body: form,
    onUploadProgress: opts?.onProgress,
    signal: opts?.signal,
  });
}

/** POST a JSON body and read the response as a Blob (streamed exports).
 * `onProgress` reports bytes *received* — for a chunked response with no
 * `Content-Length` its `total` is null, which the UI renders as an
 * indeterminate bar rather than a percentage. */
export function fetchBlob(path: string, body: unknown, opts?: TransferOptions): Promise<Blob> {
  return xhrRequest<Blob>({
    method: "POST",
    path,
    url: apiUrl(path),
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
    responseType: "blob",
    onDownloadProgress: opts?.onProgress,
    signal: opts?.signal,
  });
}

/** GET a resource as a Blob, reporting download progress. */
export function fetchBlobGet(
  path: string,
  params?: QueryParams,
  opts?: TransferOptions,
): Promise<Blob> {
  return xhrRequest<Blob>({
    method: "GET",
    path,
    url: apiUrl(path, params),
    responseType: "blob",
    onDownloadProgress: opts?.onProgress,
    signal: opts?.signal,
  });
}
