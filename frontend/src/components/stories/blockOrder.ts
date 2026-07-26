import type { StoryBlock } from "@/api/types";

/**
 * Blocks in document order (server positions ascending).
 *
 * `id` breaks ties, matching the server's ordering, so the document never
 * reshuffles between polls on a database written before positions were
 * unique per story.
 */
export function sortBlocks(blocks: StoryBlock[]): StoryBlock[] {
  return [...blocks].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
}

/**
 * Apply a move to a local block list, for the optimistic reorder.
 *
 * Mirrors the server's `after_block_id` semantics (null = top of document)
 * so the optimistic order matches what the server will return — otherwise
 * the block visibly jumps twice.
 */
export function reorderLocally(
  sorted: StoryBlock[],
  movingId: string,
  afterBlockId: string | null,
): StoryBlock[] {
  const moving = sorted.find((b) => b.id === movingId);
  if (!moving) return sorted;
  const others = sorted.filter((b) => b.id !== movingId);
  if (afterBlockId === null) return [moving, ...others];
  const anchor = others.findIndex((b) => b.id === afterBlockId);
  if (anchor < 0) return sorted;
  return [...others.slice(0, anchor + 1), moving, ...others.slice(anchor + 1)];
}

/**
 * Translate "the user dropped block `movingId` at index `targetIndex`" into
 * the `after_block_id` the move API expects (null = top of document).
 *
 * `sorted` is the current document order *including* the moving block;
 * `targetIndex` is the index the block should occupy after the move. The
 * anchor is whatever block precedes that slot once the moving block is
 * taken out of the list.
 */
export function afterIdForIndex(
  sorted: Pick<StoryBlock, "id">[],
  targetIndex: number,
  movingId: string,
): string | null {
  const others = sorted.filter((b) => b.id !== movingId);
  const clamped = Math.max(0, Math.min(targetIndex, others.length));
  if (clamped === 0) return null;
  return others[clamped - 1].id;
}
