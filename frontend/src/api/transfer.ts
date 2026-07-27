import { post, fetchBlobGet, postForm, type TransferOptions } from "./client";
import { triggerDownload } from "@/lib/download";

export const transferApi = {
  startExport: (caseId: string, includeBlobs: boolean) =>
    post<{ job_id: string }>(`/cases/${caseId}/export`, { include_blobs: includeBlobs }),

  downloadExport: async (
    caseId: string,
    jobId: string,
    caseName: string,
    opts?: TransferOptions,
  ): Promise<void> => {
    const blob = await fetchBlobGet(
      `/cases/${caseId}/export/${jobId}/download`,
      undefined,
      opts,
    );
    triggerDownload(blob, `${caseName}.vestigo`);
  },

  startImport: (file: File, opts?: TransferOptions) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<{ job_id: string }>("/cases/import", form, opts);
  },

  // Polling a transfer job is `jobsApi.get` — deliberately not duplicated here.
  // The import dialog and the job tray must poll it under the *same* query key
  // (`["job", id]`) so TanStack collapses them into one request stream rather
  // than two clients asking about the same job on two schedules.
};
