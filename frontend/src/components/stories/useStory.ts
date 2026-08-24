/**
 * The one declaration of a story's `getWithBlocks` query.
 *
 * The page and the editor both read the same story, and both used to declare
 * the key themselves with different options — only the editor set a poll
 * interval. React Query merges observers on a shared key, so the effective
 * behaviour ("poll while the editor is mounted") was right by accident and
 * read as a bug in both files. One hook, one set of options.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { storiesApi } from "@/api/stories";

/** Poll interval for the collaborative view of a story (no WebSockets). */
export const STORY_POLL_MS = 10_000;

export function storyQueryKey(caseId: string | undefined, storyId: string | undefined) {
  return ["story", caseId, storyId] as const;
}

/**
 * @param poll - Whether this consumer needs the collaborative poll. The editor
 *   does (a collaborator's edit must land without a reload); a read-only
 *   consumer does not, and asking for it would poll the whole story on every
 *   route that merely reads the title.
 */
export function useStory(
  caseId: string | undefined,
  storyId: string | undefined,
  { poll = false }: { poll?: boolean } = {},
) {
  return useQuery({
    queryKey: storyQueryKey(caseId, storyId),
    queryFn: () => storiesApi.getWithBlocks(caseId!, storyId!),
    enabled: !!caseId && !!storyId,
    refetchInterval: poll ? STORY_POLL_MS : false,
  });
}

/** Invalidate a story everywhere it is observed. */
export function useInvalidateStory(caseId: string | undefined, storyId: string | undefined) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: storyQueryKey(caseId, storyId) });
}
