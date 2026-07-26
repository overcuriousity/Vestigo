import { del, get, patch, post } from "./client";
import type {
  Story,
  StoryBlock,
  StoryBlockKind,
  StoryExportMeta,
  StorySnapshot,
} from "./types";

/** A story plus its blocks in document order — the story-editor query's shape. */
export interface StoryWithBlocks {
  story: Story;
  blocks: StoryBlock[];
}

export const storiesApi = {
  list: (caseId: string) =>
    get<{ stories: Story[] }>(`/cases/${caseId}/stories`).then((r) => r.stories),

  create: (caseId: string, title: string, description?: string) =>
    post<{ story: Story }>(`/cases/${caseId}/stories`, { title, description }).then(
      (r) => r.story,
    ),

  getWithBlocks: (caseId: string, storyId: string) =>
    get<StoryWithBlocks>(`/cases/${caseId}/stories/${storyId}`),

  update: (caseId: string, storyId: string, body: { title?: string; description?: string }) =>
    patch<{ story: Story }>(`/cases/${caseId}/stories/${storyId}`, body).then((r) => r.story),

  delete: (caseId: string, storyId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/stories/${storyId}`),

  createBlock: (
    caseId: string,
    storyId: string,
    body: {
      kind: StoryBlockKind;
      content: Record<string, unknown>;
      after_block_id?: string | null;
    },
  ) =>
    post<{ block: StoryBlock }>(`/cases/${caseId}/stories/${storyId}/blocks`, body).then(
      (r) => r.block,
    ),

  updateBlock: (
    caseId: string,
    storyId: string,
    blockId: string,
    content: Record<string, unknown>,
    version: number,
  ) =>
    patch<{ block: StoryBlock }>(`/cases/${caseId}/stories/${storyId}/blocks/${blockId}`, {
      content,
      version,
    }).then((r) => r.block),

  moveBlock: (
    caseId: string,
    storyId: string,
    blockId: string,
    afterBlockId: string | null,
    version: number,
  ) =>
    post<{ block: StoryBlock }>(
      `/cases/${caseId}/stories/${storyId}/blocks/${blockId}/move`,
      { after_block_id: afterBlockId, version },
    ).then((r) => r.block),

  deleteBlock: (caseId: string, storyId: string, blockId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/stories/${storyId}/blocks/${blockId}`),

  createExport: (caseId: string, storyId: string) =>
    post<{ export: StoryExportMeta & { snapshot: StorySnapshot } }>(
      `/cases/${caseId}/stories/${storyId}/exports`,
    ).then((r) => r.export),

  uploadArtifact: (caseId: string, storyId: string, exportId: string, html: string) =>
    post<{ export: StoryExportMeta }>(
      `/cases/${caseId}/stories/${storyId}/exports/${exportId}/artifact`,
      { html },
    ).then((r) => r.export),

  /**
   * Fetch a stored export's frozen snapshot.
   *
   * The listing omits snapshots (they are large), so re-rendering the HTML
   * artifact for an export whose upload failed has to read it back.
   */
  getSnapshot: (caseId: string, storyId: string, exportId: string) =>
    get<StorySnapshot>(`/cases/${caseId}/stories/${storyId}/exports/${exportId}/snapshot`),

  listExports: (caseId: string, storyId: string) =>
    get<{ exports: StoryExportMeta[] }>(`/cases/${caseId}/stories/${storyId}/exports`).then(
      (r) => r.exports,
    ),
};
