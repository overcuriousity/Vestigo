/**
 * Human copy for a job's coarse `progress.phase`.
 *
 * Lives here rather than in `ui/JobStatusRow` so that row stays purely
 * presentational: callers resolve the label and pass it as `detail`.
 *
 * The map is keyed on `job.kind` because export and import share phase tokens
 * (`postgres`, `events`, `blobs`) while meaning opposite directions — packing
 * vs. restoring. Rendering one copy for both would actively mislead.
 * Sources: `src/vestigo/transfer/exporter.py`, `.../importer.py`.
 */
import type { Job } from "@/api/types";

const EXPORT_PHASES: Record<string, string> = {
  queued: "Queued",
  postgres: "Collecting case records",
  events: "Packing events",
  blobs: "Packing original source files",
  manifest: "Sealing and hashing the archive",
};

const IMPORT_PHASES: Record<string, string> = {
  queued: "Queued",
  verify: "Verifying archive integrity",
  postgres: "Restoring case records",
  events: "Restoring events",
  blobs: "Restoring original source files",
  stats: "Recomputing counts",
};

const PHASES_BY_KIND: Record<string, Record<string, string>> = {
  case_export: EXPORT_PHASES,
  case_import: IMPORT_PHASES,
};

/**
 * Resolve `progress.phase` to display copy, or null when there is nothing
 * trustworthy to show — an unknown job kind or an unrecognized token. A raw
 * phase token is never surfaced: it would read as a leaked internal.
 */
export function jobPhaseLabel(
  kind: string | undefined,
  progress: Job["progress"],
): string | null {
  const phase = progress?.phase;
  if (!kind || !phase) return null;
  return PHASES_BY_KIND[kind]?.[phase] ?? null;
}
