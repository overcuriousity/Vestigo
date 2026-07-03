/**
 * Job tray store — tracks active ingest/embed jobs across route changes.
 * Jobs are polled from /api/jobs/{id} until terminal.
 */
import { create } from "zustand";
import type { Job } from "@/api/types";

export interface TrackedJob extends Job {
  label: string;
  dismissed: boolean;
  /** TanStack Query keys to invalidate when the job completes — e.g.
   * `[["sources", caseId]]` for an ingest job so the source list refreshes
   * with the final event count. */
  invalidate?: unknown[][];
}

interface JobsState {
  jobs: Record<string, TrackedJob>;
  addJob: (id: string, label: string, invalidate?: unknown[][]) => void;
  updateJob: (job: Job) => void;
  dismiss: (id: string) => void;
}

export const useJobsStore = create<JobsState>((set) => ({
  jobs: {},

  addJob: (id, label, invalidate) =>
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
        },
      },
    })),

  updateJob: (job) =>
    set((s) => {
      const existing = s.jobs[job.id];
      if (!existing) return s;
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
