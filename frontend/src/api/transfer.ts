import {
  get,
  post,
  getBlobWithProgress,
  postFormWithProgress,
  type ProgressHandler,
} from "./client";
import { triggerDownload } from "@/lib/download";
import type { Job } from "./types";

/** Byte-progress + cancellation options for the two archive transfers. */
export interface TransferOptions {
  onProgress?: ProgressHandler;
  signal?: AbortSignal;
}

export const transferApi = {
  startExport: (caseId: string, includeBlobs: boolean) =>
    post<{ job_id: string }>(`/cases/${caseId}/export`, { include_blobs: includeBlobs }),

  downloadExport: async (
    caseId: string,
    jobId: string,
    caseName: string,
    opts?: TransferOptions,
  ): Promise<void> => {
    const blob = await getBlobWithProgress(`/cases/${caseId}/export/${jobId}/download`, opts);
    triggerDownload(blob, `${caseName}.vestigo`);
  },

  startImport: (file: File, opts?: TransferOptions) => {
    const form = new FormData();
    form.append("file", file);
    // XHR rather than fetch: a multi-GB archive upload must report progress,
    // and be abortable before the server creates a job (see ImportCaseDialog).
    return postFormWithProgress<{ job_id: string }>("/cases/import", form, opts);
  },

  getJob: (jobId: string) => get<{ job: Job }>(`/jobs/${jobId}`).then((r) => r.job),
};
