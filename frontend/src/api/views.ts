import { del, get, post } from "./client";
import type { View } from "./types";

export const viewsApi = {
  list: (caseId: string) =>
    get<{ views: View[] }>(`/cases/${caseId}/views`).then((r) => r.views),

  create: (
    caseId: string,
    name: string,
    query: string,
    filter: Record<string, unknown>,
  ) =>
    post<{ view: View }>(`/cases/${caseId}/views`, { name, query, filter }).then(
      (r) => r.view,
    ),

  /** `hidden` is true when a story block still embeds the view: the row is
   *  kept so that story keeps rendering, and swept once the last block
   *  referencing it is gone. */
  delete: (caseId: string, viewId: string) =>
    del<{ deleted: boolean; view_id: string; hidden: boolean }>(
      `/cases/${caseId}/views/${viewId}`,
    ),
};
