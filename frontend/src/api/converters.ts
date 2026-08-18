import { BASE, get, post, postForm } from "./client";
import type { TransferOptions } from "./client";

export interface ConverterInfo {
  name: string;
  filename: string;
  description: string;
  inputs: string[];
  size_bytes: number;
  sha256: string;
  /** In-repo native converter (e.g. Parquet output), not vendored from upstream. */
  native?: boolean;
  /** Non-stdlib Python dependencies the script needs (e.g. pyarrow). */
  requires?: string[];
}

export interface ConverterManifest {
  upstream: string;
  commit: string;
  version: string;
  license: string;
  converters: ConverterInfo[];
}

export interface ConverterCheck {
  name: string;
  ok: boolean;
  detail: string;
  enforced: boolean;
}

export interface ConverterAttempt {
  n: number;
  phase: "generate" | "sample" | "full" | "ingest";
  model: string | null;
  elapsed_ms: number;
  exit_code: number | null;
  stderr_tail: string;
  error?: string | null;
  /** Hash of the prompt this round sent (generate/sample entries), so the trail
   * names the exact prompt behind each draft. */
  prompt_hash?: string | null;
  validation: { ok: boolean; rows: number; checks: ConverterCheck[] } | null;
}

/** A model-written converter script bound to one case
 * (`src/vestigo/db/postgres.py::ConverterScript`). */
export interface ConverterScript {
  id: string;
  case_id: string;
  name: string;
  version: number;
  parent_id: string | null;
  status: "generating" | "working" | "failed";
  model: string | null;
  provider_endpoint: string | null;
  prompt_hash: string | null;
  sample_hash: string | null;
  raw_file_hash: string;
  raw_filename: string | null;
  /** The evidence file's own mtime as stated to the model, or null ("unknown"). */
  raw_mtime?: string | null;
  hint: string | null;
  attempts: ConverterAttempt[];
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** List endpoint only. */
  sources_produced?: number;
  /** Detail endpoint only. */
  source_code?: string | null;
  sample_excerpt?: string | null;
}

export interface CaseConvertersResponse {
  scripts: ConverterScript[];
  /** Bytes of the raw file the generation sends to the model. */
  sample_bytes: number;
}

export interface ConvertStartResult {
  job_id: string;
  converter_script_id: string | null;
}

export const convertersApi = {
  list: () => get<ConverterManifest>("/converters"),

  /** Copy-paste LLM prompts, rendered server-side from the data contract. */
  prompts: () => get<{ parquet: string; csv: string }>("/converters/prompt"),

  downloadUrl: (name: string) => `${BASE}/converters/${name}`,

  // ── Generated converters (case-bound) ─────────────────────────────────

  listForCase: (caseId: string) =>
    get<CaseConvertersResponse>(`/cases/${caseId}/converters`),

  getForCase: (caseId: string, id: string) =>
    get<ConverterScript>(`/cases/${caseId}/converters/${id}`),

  caseDownloadUrl: (caseId: string, id: string) =>
    `${BASE}/cases/${caseId}/converters/${id}/download`,

  /** Upload a plain-text log; the model writes the converter (or a saved one re-runs). */
  convert: (
    caseId: string,
    file: File,
    opts: { hint?: string; converterScriptId?: string },
    xfer?: TransferOptions,
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (opts.hint) form.append("hint", opts.hint);
    if (opts.converterScriptId) form.append("converter_script_id", opts.converterScriptId);
    // The evidence file's own mtime: what the model is told and what the
    // script sees on its input. Without it the server's copy would only
    // know the upload time.
    if (file.lastModified > 0) form.append("mtime", String(file.lastModified / 1000));
    return postForm<ConvertStartResult>(`/cases/${caseId}/converters/convert`, form, xfer);
  },

  regenerate: (caseId: string, id: string, hint?: string) =>
    post<{ job_id: string }>(`/cases/${caseId}/converters/${id}/regenerate`, {
      hint: hint ?? null,
    }),
};
