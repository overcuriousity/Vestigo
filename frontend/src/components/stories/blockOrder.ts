import type { StoryBlock } from "@/api/types";

/** Blocks in document order (server positions ascending). */
export function sortBlocks(blocks: StoryBlock[]): StoryBlock[] {
  return [...blocks].sort((a, b) => a.position - b.position);
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
