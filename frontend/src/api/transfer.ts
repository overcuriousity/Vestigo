import { get, post, postForm, fetchBlobGet } from "./client";
import { triggerDownload } from "@/lib/download";
import type { Job } from "./types";

export const transferApi = {
  startExport: (caseId: string, includeBlobs: boolean) =>
    post<{ job_id: string }>(`/cases/${caseId}/export?include_blobs=${includeBlobs}`),

  downloadExport: async (caseId: string, jobId: string, caseName: string): Promise<void> => {
    const blob = await fetchBlobGet(`/cases/${caseId}/export/${jobId}/download`);
    triggerDownload(blob, `${caseName}.vestigo`);
  },

  startImport: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<{ job_id: string }>("/cases/import", form);
  },

  getJob: (jobId: string) => get<{ job: Job }>(`/jobs/${jobId}`).then((r) => r.job),
};
