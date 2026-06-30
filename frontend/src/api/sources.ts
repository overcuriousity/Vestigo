import { del, get, post, postForm } from "./client";
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

  upload: (
    caseId: string,
    file: File,
    name?: string,
    parser?: string,
  ): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    if (parser) form.append("parser", parser);
    return postForm<UploadResult>(`/cases/${caseId}/sources`, form);
  },

  downloadUrl: (caseId: string, sourceId: string) =>
    `/api/cases/${caseId}/sources/${sourceId}/download`,

  embed: (
    caseId: string,
    sourceId: string,
    embeddingConfig?: { version: 1; artifacts: Record<string, string[]> },
  ) =>
    post<{ job_id: string; status: string }>(
      `/cases/${caseId}/sources/${sourceId}/embed`,
      embeddingConfig != null
        ? { embedding_config: embeddingConfig }
        : undefined,
    ),
};
