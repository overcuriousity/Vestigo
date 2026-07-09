import { get } from "./client";
import type { Job } from "./types";

export const jobsApi = {
  get: (jobId: string) => get<{ job: Job }>(`/jobs/${jobId}`).then((r) => r.job),
  listByCase: (caseId: string) =>
    get<{ jobs: Job[] }>(`/cases/${caseId}/jobs`).then((r) => r.jobs),
};
