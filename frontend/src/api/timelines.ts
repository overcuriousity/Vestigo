import { del, get, post, postForm } from "./client";
import type { EmbeddingFieldConfig, Timeline, UploadResult } from "./types";

export const timelinesApi = {
  list: (caseId: string) =>
    get<{ timelines: Timeline[] }>(`/cases/${caseId}/timelines`).then(
      (r) => r.timelines,
    ),

  get: (caseId: string, timelineId: string) =>
    get<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}`,
    ).then((r) => r.timeline),

  create: (caseId: string, name: string, description?: string) =>
    post<{ timeline: Timeline }>(`/cases/${caseId}/timelines`, {
      name,
      description,
    }).then((r) => r.timeline),

  delete: (caseId: string, timelineId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/timelines/${timelineId}`),

  upload: (
    caseId: string,
    timelineId: string,
    file: File,
    parser?: string,
  ): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file);
    if (parser) form.append("parser", parser);
    return postForm<UploadResult>(
      `/cases/${caseId}/timelines/${timelineId}/upload`,
      form,
    );
  },

  embed: (
    caseId: string,
    timelineId: string,
    embeddingConfig?: EmbeddingFieldConfig,
  ) =>
    post<{ job_id: string; status: string }>(
      `/cases/${caseId}/timelines/${timelineId}/embed`,
      embeddingConfig != null ? { embedding_config: embeddingConfig } : undefined,
    ),
};
