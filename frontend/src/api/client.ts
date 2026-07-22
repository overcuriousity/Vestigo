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

/** Shared 401-handling + error-surfacing for every fetch helper below.
 * Excludes `/auth/login` from `onUnauthorized` — a 401 there is just a
 * rejected login attempt, not a session that needs to redirect to itself. */
async function checkResponse(res: Response, path: string): Promise<void> {
  if (res.status === 401 && path !== "/auth/login") {
    onUnauthorized?.();
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = extractErrorDetail(j, detail);
    } catch {
      // ignore
    }
    throw new ApiError(res.status, detail);
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
