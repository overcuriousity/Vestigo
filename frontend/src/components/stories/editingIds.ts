/**
 * Which blocks are under active edit, as a state value the editor can hold.
 *
 * Its own module rather than a `StoryEditor` export: a component file that
 * also exports plain functions loses fast refresh (and oxlint says so).
 */

/**
 * The next set of block ids under edit — the *same* Set when membership does
 * not change.
 *
 * Returning a fresh Set every time denies React its `Object.is` bail-out, so
 * any caller reporting the same state twice re-renders the whole story. That
 * was one half of the #193 render loop; it lives here so it can be pinned on
 * its own, since `MarkdownBlock`'s callback ref alone would also stop the
 * loop and hide a regression in this half.
 */
export function nextEditingIds(
  prev: ReadonlySet<string>,
  blockId: string,
  editing: boolean,
): Set<string> {
  if (prev.has(blockId) === editing) return prev as Set<string>;
  const next = new Set(prev);
  if (editing) next.add(blockId);
  else next.delete(blockId);
  return next;
}
