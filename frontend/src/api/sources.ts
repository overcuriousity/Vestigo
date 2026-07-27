import { del, get, patch, postForm, type TransferOptions } from "./client";
import type {
  EmbeddingFieldsResponse,
  Source,
  UploadResult,
} from "./types";

export const sourcesApi = {
  embeddingFields: (caseId: string, sourceId: string) =>
    get<EmbeddingFieldsResponse>(
      `/cases/${caseId}/sources/${sourceId}/embedding-fields`,
    ),

  list: (caseId: string) =>
    get<{ sources: Source[] }>(`/cases/${caseId}/sources`).then(
      (r) => r.sources,
    ),

  get: (caseId: string, sourceId: string) =>
    get<{ source: Source }>(`/cases/${caseId}/sources/${sourceId}`).then(
      (r) => r.source,
    ),

  delete: (caseId: string, sourceId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/sources/${sourceId}`),

  /** Set a source's query-time clock-skew correction (W2), in seconds. */
  update: (caseId: string, sourceId: string, timeOffsetSeconds: number) =>
    patch<{ source: Source }>(`/cases/${caseId}/sources/${sourceId}`, {
      time_offset_seconds: timeOffsetSeconds,
    }).then((r) => r.source),

  /** Upload one log source. This is the app's largest routine transfer — the
   * server cap is 10 GiB — so `opts` carries byte progress and an abort
   * signal; the ingest job the tray polls only exists once the whole body has
   * landed, so everything before that is `opts.onProgress` or nothing. */
  upload: (
    caseId: string,
    file: File,
    name?: string,
    parser?: string,
    opts?: TransferOptions,
  ): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    if (parser) form.append("parser", parser);
    return postForm<UploadResult>(`/cases/${caseId}/sources`, form, opts);
  },

  downloadUrl: (caseId: string, sourceId: string) =>
    `/api/cases/${caseId}/sources/${sourceId}/download`,
};
