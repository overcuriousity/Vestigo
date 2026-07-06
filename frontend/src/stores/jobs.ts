/**
 * Job tray store — tracks active ingest/embed jobs across route changes.
 * Jobs are polled from /api/jobs/{id} until terminal.
 */
import { create } from "zustand";
import { tourEvent } from "@/stores/tour";
import type { Job } from "@/api/types";

export interface TrackedJob extends Job {
  label: string;
  dismissed: boolean;
  /** TanStack Query keys to invalidate when the job completes — e.g.
   * `[["sources", caseId]]` for an ingest job so the source list refreshes
   * with the final event count. */
  invalidate?: unknown[][];
  /** The onboarding tour's "ingesting" step is waiting on this specific job —
   * set only by the upload flow the tour itself drove, so an unrelated job
   * (another case, a second concurrent upload) can't advance the tour early. */
  tourTracked?: boolean;
}

interface JobsState {
  jobs: Record<string, TrackedJob>;
  addJob: (id: string, label: string, invalidate?: unknown[][], tourTracked?: boolean) => void;
  updateJob: (job: Job) => void;
  dismiss: (id: string) => void;
}

export const useJobsStore = create<JobsState>((set) => ({
  jobs: {},

  addJob: (id, label, invalidate, tourTracked) =>
    set((s) => ({
      jobs: {
        ...s.jobs,
        [id]: {
          id,
          kind: "unknown",
          status: "queued",
          progress: null,
          result: null,
          error: null,
          label,
          dismissed: false,
          invalidate,
          tourTracked,
        },
      },
    })),

  updateJob: (job) =>
    set((s) => {
      const existing = s.jobs[job.id];
      if (!existing) return s;
      const wasTerminal = existing.status === "completed" || existing.status === "failed";
      const isTerminal = job.status === "completed" || job.status === "failed";
      // Notify the onboarding tour when the job it's specifically waiting on
      // finishes (the "ingesting" step waits on this; no-op otherwise).
      // Failed counts too — the tour must not deadlock on a broken file.
      if (!wasTerminal && isTerminal && existing.tourTracked) tourEvent("ingest-complete");
      return {
        jobs: {
          ...s.jobs,
          [job.id]: { ...existing, ...job },
        },
      };
    }),

  dismiss: (id) =>
    set((s) => {
      const existing = s.jobs[id];
      if (!existing) return s;
      return {
        jobs: { ...s.jobs, [id]: { ...existing, dismissed: true } },
      };
    }),
}));
