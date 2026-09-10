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

  /** Delete a source and its events/vectors.
   *
   * Refuses with 409 when any analyst-created timeline still lists the source
   * — deleting it would silently rewrite that grouping and everything declared
   * over it. `force` is the analyst's confirmation of exactly that, so the
   * dialog sends it only after showing which timelines are affected. The
   * default "All sources" timeline never triggers the refusal. */
  delete: (caseId: string, sourceId: string, force = false) =>
    del<{ deleted: boolean; removed_from_timelines: string[] }>(
      `/cases/${caseId}/sources/${sourceId}`,
      force ? { force: true } : undefined,
    ),

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
